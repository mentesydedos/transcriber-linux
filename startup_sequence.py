#!/usr/bin/env python3
"""
startup_sequence.py — Arranque escalonado y verificado de todo AlertaTV.

Codifica, en un solo lugar, exactamente el mismo procedimiento cuidadoso
que se siguió a mano durante el incidente de OOM de CUDA documentado en
transcriber_ctc_es.py/transcriber_parakeet.py (2026-08-26): nunca arrancar
los dos motores de GPU (TV y radio) al mismo tiempo ni casi al mismo tiempo
-- uno primero, verificar que de verdad quedó sano (no solo "systemctl dice
que está activo"), y solo entonces el siguiente. Un arranque simultáneo de
ambos causó fallas de asignación de memoria CUDA la única vez que se probó
así en esta sesión (2026-09-07).

Se corre como servicio systemd (systemd/alertatv-startup.service) al
arrancar la máquina, así que corre como root -- no necesita sudo interactivo
para controlar los demás servicios via `systemctl`. También se puede correr
a mano para reintentar un arranque o para auditar el estado del sistema:

    sudo python3 startup_sequence.py

Progreso: se imprime a stdout (queda en journalctl -u alertatv-startup.service
-f) Y se escribe a STATUS_FILE en cada paso, en JSON, para que el dashboard
web (una vez que alerts.service esté arriba) pueda mostrarlo -- ver
alerts/app.py:admin_startup_status.

Filosofía de validación: "el servicio está activo" NO es suficiente (ver el
incidente -- transcriber-parakeet.service se quedó "active" mientras fallaba
en CADA fragmento sin producir nada). Cada paso crítico verifica además:
ausencia de errores nuevos en el log, memoria GPU en un rango sano, y --
para los motores de transcripción -- que el timestamp más reciente en
transcriptions.db de verdad esté avanzando.
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from system_health import (
    run, is_active, recent_errors, gpu_free_mib, latest_transcription_ts, check_http,
    GPU_MIN_FREE_MIB_WARN,
)

BASE_DIR = Path(__file__).parent
STATUS_FILE = BASE_DIR / "logs" / "startup_status.json"
STATUS_FILE.parent.mkdir(exist_ok=True)

# Cuánto esperar a que un motor de transcripción produzca su primer
# fragmento real después de arrancar, antes de darlo por bueno -- Parakeet
# tarda ~15s solo en cargar el modelo (ver TimeoutStartSec en el .service),
# más el tiempo de conectar las estaciones/canales escalonado. Ventana más
# larga que la de la prueba de salud manual (system_health.py) a propósito:
# aquí SÍ hay que cubrir el arranque en frío del modelo, no solo confirmar
# que algo ya corriendo sigue avanzando.
ENGINE_WARMUP_SEC = 90
ENGINE_POLL_SEC = 5


def log(msg: str, level: str = "info") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [{level.upper()}] {msg}", flush=True)


_status = {"started_at": None, "finished_at": None, "ok": None, "steps": []}


def _save_status():
    STATUS_FILE.write_text(json.dumps(_status, indent=2, ensure_ascii=False))


def step(name: str, ok: bool, detail: str = "") -> None:
    _status["steps"].append({
        "name": name, "ok": ok, "detail": detail,
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    _save_status()
    log(f"{'OK' if ok else 'FALLO'} -- {name}{(': ' + detail) if detail else ''}",
        "info" if ok else "error")


def systemctl_start(unit: str) -> bool:
    r = run(["systemctl", "start", unit])
    if r.returncode != 0:
        step(f"iniciar {unit}", False, r.stderr.strip()[:300])
        return False
    return True


def start_and_verify_gpu_engine(unit: str, channel_min: int, channel_max: int, label: str) -> bool:
    """Arranca un motor de transcripción de GPU y NO avanza hasta confirmar
    que de verdad está produciendo transcripciones nuevas, sin errores de
    CUDA -- el chequeo que hubiera atrapado el incidente del 2026-09-07
    (servicio "active" pero sin producir nada) antes de arrancar el
    siguiente motor de GPU encima."""
    log(f"Arrancando {label} ({unit})...")
    before_ts = latest_transcription_ts(channel_min, channel_max)
    start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not systemctl_start(unit):
        return False
    time.sleep(3)
    if not is_active(unit):
        step(f"{label} arrancó", False, "systemctl lo reporta inactivo justo después de iniciar")
        return False

    deadline = time.time() + ENGINE_WARMUP_SEC
    while time.time() < deadline:
        time.sleep(ENGINE_POLL_SEC)
        if not is_active(unit):
            step(f"{label} se mantuvo activo", False, "el servicio se cayó durante el arranque")
            return False
        errs = recent_errors(unit, start_time, ["cuda out of memory", "cublas", "traceback"])
        now_ts = latest_transcription_ts(channel_min, channel_max)
        advanced = now_ts is not None and now_ts != before_ts
        if advanced and not errs:
            gpu_free = gpu_free_mib()
            detail = f"transcribiendo (última: {now_ts})"
            if gpu_free is not None:
                detail += f", GPU libre: {gpu_free} MiB"
                if gpu_free < GPU_MIN_FREE_MIB_WARN:
                    log(f"  aviso: margen de GPU libre bajo ({gpu_free} MiB) tras {label}", "warn")
            step(f"{label} verificado", True, detail)
            return True
        if errs:
            # Puede ser un error transitorio del arranque escalonado (ya
            # visto en producción: un pico único mientras todos los canales
            # conectan) -- no se corta de inmediato, se sigue esperando
            # dentro de la ventana, pero se deja constancia.
            log(f"  {label}: {len(errs)} línea(s) de error CUDA detectadas, sigue esperando...", "warn")

    # Se acabó la ventana de espera sin confirmar avance real.
    final_errs = recent_errors(unit, start_time, ["cuda out of memory", "cublas", "traceback"])
    step(f"{label} verificado", False,
         f"sin transcripción nueva confirmada en {ENGINE_WARMUP_SEC}s"
         + (f", {len(final_errs)} error(es) CUDA en el log" if final_errs else ""))
    return False


def start_simple(unit: str, label: str, wait: int = 5) -> bool:
    log(f"Arrancando {label} ({unit})...")
    if not systemctl_start(unit):
        return False
    time.sleep(wait)
    ok = is_active(unit)
    step(label, ok, "" if ok else "no quedó activo")
    return ok


def check_http(url: str, label: str, timeout: int = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", url], timeout=5)
            if r.stdout.strip() in ("200", "302"):
                step(label, True, f"responde {r.stdout.strip()}")
                return True
        except Exception:
            pass
        time.sleep(2)
    step(label, False, "no respondió a tiempo")
    return False


def main() -> int:
    _status["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _status["ok"] = None
    _status["steps"] = []
    _save_status()
    log("═══ Arranque de AlertaTV -- inicio ═══")

    all_ok = True

    # 1) Detector de música -- aislado, sin GPU, que esté listo antes de que
    # los motores de transcripción intenten hablarle (fallan en silencio
    # si no está, pero mejor que ya esté arriba).
    all_ok &= start_simple("music-classifier.service", "Detector de música (aislado, CPU)")

    # 2) Captura de audio/video -- sin GPU, independientes entre sí.
    all_ok &= start_simple("radio-recorder.service", "Grabación de audio de radio")
    all_ok &= start_simple("transcriber-video-recorder.service", "Grabación de video de TV")

    # 3) Motores de GPU -- SIEMPRE uno a la vez, con verificación real entre
    # cada uno. Este orden (TV primero) es el mismo que se usó para
    # recuperarse del incidente real.
    ok_tv = start_and_verify_gpu_engine("transcriber-ctc-es.service", 1, 26, "Transcripción de TV (GPU)")
    all_ok &= ok_tv
    if ok_tv:
        all_ok &= start_and_verify_gpu_engine("transcriber-parakeet.service", 27, 999, "Transcripción de radio (GPU)")
    else:
        log("TV no quedó sano -- NO se arranca radio encima para no repetir el incidente de OOM.", "error")
        step("Transcripción de radio (GPU)", False, "omitido -- TV no pasó su verificación")
        all_ok = False

    # 4) Aplicación web -- sin GPU.
    all_ok &= start_simple("alerts.service", "Panel web (AlertaTV)")
    all_ok &= start_simple("alerts-stream.service", "Streaming (Videoteca/Audioteca en vivo)")
    all_ok &= start_simple("alerts-watcher.service", "Watcher de búsquedas y alertas")
    if all_ok:
        check_http("http://127.0.0.1:8001/login", "Panel web responde")

    # 5) RAG (menor prioridad, no bloquea el resto).
    start_simple("rag-api.service", "API de preguntas en lenguaje natural (RAG)")

    _status["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _status["ok"] = bool(all_ok)
    _save_status()

    log("═══ Resumen ═══")
    for s in _status["steps"]:
        log(f"  [{'OK' if s['ok'] else 'FALLO'}] {s['name']}" + (f" -- {s['detail']}" if s['detail'] else ''))
    log(f"═══ Arranque {'COMPLETO Y VALIDADO' if all_ok else 'TERMINÓ CON PROBLEMAS -- revisar arriba'} ═══",
        "info" if all_ok else "error")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
