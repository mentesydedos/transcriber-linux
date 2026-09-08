"""
system_health.py — Verificaciones reales del estado de AlertaTV, compartidas
entre startup_sequence.py (arranque escalonado) y la prueba de salud manual
desde /admin/startup (alerts/app.py). Aquí solo se VERIFICA -- nada de esto
inicia, detiene ni reinicia ningún servicio.

Mismo criterio que en startup_sequence.py: "el servicio está activo" no es
suficiente por sí solo (ver el incidente de OOM del 2026-08-26, donde
transcriber-parakeet.service se quedó "active" sin producir nada) -- cada
motor de transcripción se valida confirmando que su timestamp más reciente
de verdad avanza, no solo que systemd lo reporte corriendo.
"""
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
TRANS_DB = BASE_DIR / "transcriptions.db"
HEALTH_STATUS_FILE = BASE_DIR / "logs" / "health_check_status.json"
HEALTH_STATUS_FILE.parent.mkdir(exist_ok=True)

GPU_MIN_FREE_MIB_WARN = 50

# Para una prueba manual (todo YA debería estar corriendo, sin necesidad de
# esperar a que cargue el modelo) alcanza una ventana corta para confirmar
# avance real -- a diferencia de ENGINE_WARMUP_SEC (90s) en
# startup_sequence.py, que sí necesita cubrir el tiempo de carga del modelo
# recién arrancado.
HEALTH_CHECK_WINDOW_SEC = 12


def run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def is_active(unit: str) -> bool:
    r = run(["systemctl", "is-active", unit])
    return r.stdout.strip() == "active"


def recent_errors(unit: str, since: str, patterns: list[str]) -> list[str]:
    r = run(["journalctl", "-u", unit, "--since", since, "--no-pager"], timeout=15)
    hits = []
    for pat in patterns:
        hits += [l for l in r.stdout.splitlines() if pat.lower() in l.lower()]
    return hits


def gpu_free_mib() -> int | None:
    try:
        r = run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"])
        return int(r.stdout.strip().splitlines()[0])
    except Exception:
        return None


def latest_transcription_ts(channel_min: int, channel_max: int) -> str | None:
    try:
        r = run([str(BASE_DIR / "venv" / "bin" / "python3"), "-c", f"""
import sqlite3
conn = sqlite3.connect({str(TRANS_DB)!r})
row = conn.execute("SELECT MAX(timestamp) FROM transcriptions WHERE channel_id BETWEEN {channel_min} AND {channel_max}").fetchone()
print(row[0] or '')
"""], timeout=15)
        return r.stdout.strip() or None
    except Exception:
        return None


def check_http(url: str, timeout: int = 8) -> tuple[bool, str]:
    try:
        r = run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", url], timeout=timeout)
        code = r.stdout.strip()
        return code in ("200", "302"), f"responde {code}" if code else "sin respuesta"
    except Exception as e:
        return False, str(e)


def check_gpu_engine(unit: str, channel_min: int, channel_max: int, label: str,
                      window_sec: int = HEALTH_CHECK_WINDOW_SEC) -> dict:
    """Para un motor YA corriendo: confirma que avanza de verdad en una
    ventana corta, sin errores CUDA recientes. No arranca ni reinicia nada."""
    if not is_active(unit):
        return {"name": label, "ok": False, "detail": "servicio inactivo"}

    since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    before_ts = latest_transcription_ts(channel_min, channel_max)
    time.sleep(window_sec)
    after_ts = latest_transcription_ts(channel_min, channel_max)
    errs = recent_errors(unit, since, ["cuda out of memory", "cublas", "traceback"])
    advanced = after_ts is not None and after_ts != before_ts

    gpu_free = gpu_free_mib()
    detail_parts = [f"última transcripción: {after_ts or 'sin datos'}"]
    if gpu_free is not None:
        detail_parts.append(f"GPU libre: {gpu_free} MiB")
    if errs:
        detail_parts.append(f"{len(errs)} error(es) CUDA en los últimos {window_sec}s")

    ok = advanced and not errs
    if not advanced:
        detail_parts.insert(0, "SIN avance de transcripción")
    return {"name": label, "ok": ok, "detail": ", ".join(detail_parts)}


def check_simple_service(unit: str, label: str) -> dict:
    ok = is_active(unit)
    return {"name": label, "ok": ok, "detail": "" if ok else "servicio inactivo"}


def run_full_health_check() -> dict:
    """Corre TODAS las verificaciones sobre lo que ya está corriendo, sin
    iniciar/detener nada. Pensado para el botón de "prueba de salud" en
    /admin/startup -- toma ~{HEALTH_CHECK_WINDOW_SEC*2 + unos segundos}
    porque valida TV y radio en paralelo lógico (una ventana de espera para
    cada una, no dos esperas seguidas)."""
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    steps = [
        check_simple_service("music-classifier.service", "Detector de música (aislado, CPU)"),
        check_simple_service("radio-recorder.service", "Grabación de audio de radio"),
        check_simple_service("transcriber-video-recorder.service", "Grabación de video de TV"),
    ]

    # TV y radio comparten la misma ventana de espera de verdad avanzando
    # -- se miden los timestamps "antes" de ambas primero, se espera UNA
    # vez, y se comparan los "después" de ambas, en vez de esperar la
    # ventana completa dos veces seguidas.
    tv_active = is_active("transcriber-ctc-es.service")
    radio_active = is_active("transcriber-parakeet.service")
    since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tv_before = latest_transcription_ts(1, 26) if tv_active else None
    radio_before = latest_transcription_ts(27, 999) if radio_active else None
    if tv_active or radio_active:
        time.sleep(HEALTH_CHECK_WINDOW_SEC)

    for active, unit, before, chmin, chmax, label in (
        (tv_active, "transcriber-ctc-es.service", tv_before, 1, 26, "Transcripción de TV (GPU)"),
        (radio_active, "transcriber-parakeet.service", radio_before, 27, 999, "Transcripción de radio (GPU)"),
    ):
        if not active:
            steps.append({"name": label, "ok": False, "detail": "servicio inactivo"})
            continue
        after = latest_transcription_ts(chmin, chmax)
        errs = recent_errors(unit, since, ["cuda out of memory", "cublas", "traceback"])
        advanced = after is not None and after != before
        gpu_free = gpu_free_mib()
        parts = [f"última transcripción: {after or 'sin datos'}"]
        if gpu_free is not None:
            parts.append(f"GPU libre: {gpu_free} MiB")
        if errs:
            parts.append(f"{len(errs)} error(es) CUDA recientes")
        if not advanced:
            parts.insert(0, "SIN avance de transcripción")
        steps.append({"name": label, "ok": advanced and not errs, "detail": ", ".join(parts)})

    steps.append(check_simple_service("alerts.service", "Panel web (AlertaTV)"))
    steps.append(check_simple_service("alerts-stream.service", "Streaming (Videoteca/Audioteca en vivo)"))
    steps.append(check_simple_service("alerts-watcher.service", "Watcher de búsquedas y alertas"))
    http_ok, http_detail = check_http("http://127.0.0.1:8001/login")
    steps.append({"name": "Panel web responde", "ok": http_ok, "detail": http_detail})
    steps.append(check_simple_service("rag-api.service", "API de preguntas en lenguaje natural (RAG)"))

    for s in steps:
        s["at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    status = {
        "started_at": started_at,
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ok": all(s["ok"] for s in steps),
        "steps": steps,
    }
    HEALTH_STATUS_FILE.write_text(json.dumps(status, indent=2, ensure_ascii=False))
    return status
