"""
radio_recorder.py — Graba en bloques de 30 min el audio de las 38 estaciones
de radio del M3U (ver alerts/radiowall.py:list_radio_stations), comprimido a
AAC 96kbps (mismo bitrate que ya usa el audio de video_recorder.py). Un
proceso ffmpeg por estación, supervisado (se reinicia si muere) -- mismo
patrón que video_recorder.py.

Estos bloques son solo un BUFFER DE PASO local: backup_nas2_radio.py los
sube al NAS (148.201.38.42) cada minuto y los borra de aquí en cuanto ya
quedaron ahí. El NAS es el almacén real (retención fija de 30 días, ver
cleanup_nas_radio.py) -- a diferencia de output_video/, aquí NO conviene
dejar que se acumulen localmente: el disco local ya está apretado con el
video de TV, y el audio no necesita quedarse ahí ni un minuto más de lo
que tarda en subir.

Sin ventana nocturna propia en el código -- se reutiliza el mismo horario
que video_recorder.py (00:00-05:30 hora local) por pedido explícito, para
dar el mismo hueco de mantenimiento diario.
"""
import os
import time
import signal
import logging
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from alerts.radiowall import list_radio_stations

AUDIO_DIR      = Path(os.environ.get("TRANSCRIBER_RADIO_DIR", "output_radio"))
SEGMENT_SEC    = int(os.environ.get("TRANSCRIBER_RADIO_SEGMENT_SEC", "1800"))
AUDIO_BITRATE  = os.environ.get("TRANSCRIBER_RADIO_ABITRATE", "96k")
RESTART_DELAY  = 5
STATUS_EVERY   = 60

# Cada cuántos segundos se muestrea la duración REAL del bloque en curso
# (ver drift_sampler) -- los reconexiones de ffmpeg (-reconnect) no rellenan
# con silencio el tiempo perdido, así que un bloque de 30 min puede tener
# menos audio real del que el reloj indica. audio_clips.py usa estas
# muestras para ubicar un momento dentro del archivo sin asumir que
# "segundo N del archivo" == "segundo N del bloque real" -- ver el bug real
# encontrado 2026-08-26 (LOS40 GDL, bloque de 30 min con 41s de menos).
DRIFT_SAMPLE_SEC = int(os.environ.get("TRANSCRIBER_RADIO_DRIFT_SAMPLE_SEC", "20"))

# Misma ventana nocturna que video_recorder.py (00:00-05:30 hora local).
PAUSE_START_H  = int(os.environ.get("TRANSCRIBER_RADIO_PAUSE_START_H", "0"))
PAUSE_START_M  = int(os.environ.get("TRANSCRIBER_RADIO_PAUSE_START_M", "0"))
PAUSE_END_H    = int(os.environ.get("TRANSCRIBER_RADIO_PAUSE_END_H", "5"))
PAUSE_END_M    = int(os.environ.get("TRANSCRIBER_RADIO_PAUSE_END_M", "30"))

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "radio_recorder.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("radio_recorder")

stop_event   = threading.Event()
active_procs: dict[int, subprocess.Popen] = {}
active_lock  = threading.Lock()


def _in_pause_window(now=None) -> bool:
    now = now or time.localtime()
    start = PAUSE_START_H * 60 + PAUSE_START_M
    end   = PAUSE_END_H * 60 + PAUSE_END_M
    cur   = now.tm_hour * 60 + now.tm_min
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end  # ventana que cruza medianoche


def _seconds_until_resume(now=None) -> float:
    now = now or time.localtime()
    cur = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
    end = PAUSE_END_H * 3600 + PAUSE_END_M * 60
    delta = end - cur
    return delta if delta > 0 else delta + 86400


def _seconds_until_pause(now=None) -> float:
    now = now or time.localtime()
    cur = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
    start = PAUSE_START_H * 3600 + PAUSE_START_M * 60
    delta = start - cur
    return delta if delta > 0 else delta + 86400


def pause_scheduler():
    while not stop_event.is_set():
        wait_s = _seconds_until_pause()
        if stop_event.wait(wait_s):
            break
        with active_lock:
            procs = list(active_procs.values())
        if procs:
            logger.info(f"Ventana nocturna ({PAUSE_START_H:02d}:{PAUSE_START_M:02d}"
                        f"-{PAUSE_END_H:02d}:{PAUSE_END_M:02d}): deteniendo "
                        f"{len(procs)} grabaciones activas")
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)


def _ffprobe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def _current_block_path(num: int, safe: str) -> Path | None:
    """El archivo que ffmpeg está escribiendo AHORA -- no se puede asumir
    que siempre arranca alineado a :00/:30 (segment_atclocktime), porque
    cuando el proceso ffmpeg de una estación se reinicia (falla de conexión,
    ver station_recorder) arranca un bloque nuevo con nombre "irregular" en
    ese momento. Se usa el .aac con el mtime más reciente de la carpeta en
    vez de calcular el nombre esperado -- así siempre apunta al archivo que
    de verdad está creciendo."""
    folder = AUDIO_DIR / f"canal_{num:02d}_{safe}"
    try:
        files = list(folder.glob("*.aac"))
    except OSError:
        return None
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def drift_sampler(num: int, name: str):
    """Cada DRIFT_SAMPLE_SEC, mide con ffprobe cuánto audio real lleva el
    bloque que se está grabando en este momento y lo anota (hora real,
    duración real) en un sidecar .driftlog junto al .aac -- ver comentario
    en DRIFT_SAMPLE_SEC arriba. No modifica la grabación en sí, solo la
    observa; si esto falla no afecta la grabación."""
    safe = _safe_name(name)
    while not stop_event.is_set():
        if stop_event.wait(DRIFT_SAMPLE_SEC):
            break
        if _in_pause_window():
            continue
        with active_lock:
            recording = num in active_procs
        if not recording:
            continue
        aac_path = _current_block_path(num, safe)
        if aac_path is None or not aac_path.exists():
            continue
        dur = _ffprobe_duration(aac_path)
        if dur <= 0:
            continue
        try:
            with open(aac_path.with_suffix(".driftlog"), "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat()},{dur:.3f}\n")
        except OSError:
            pass


def station_recorder(num: int, name: str, url: str, headers: str | None):
    safe = _safe_name(name)
    folder = AUDIO_DIR / f"canal_{num:02d}_{safe}"
    folder.mkdir(parents=True, exist_ok=True)
    pattern = str(folder / f"canal_{num:02d}_{safe}_%Y-%m-%d_%H-%M.aac")

    # Mismo header Origin/Referer que usa alerts/radiowall.py:stream_proxy
    # para las estaciones detrás de Zeno.fm -- sin esto, ffmpeg recibe 401
    # de la misma forma que un <audio> directo del navegador.
    header_args = []
    if headers:
        header_args = ["-headers", headers.rstrip() + "\r\n"]

    while not stop_event.is_set():
        if _in_pause_window():
            wait_s = _seconds_until_resume()
            logger.info(f"[{num:02d}] {name}: ventana nocturna, reanuda en {wait_s/60:.0f} min")
            stop_event.wait(wait_s)
            continue

        cmd = [
            "ffmpeg", "-nostdin",
            "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5", "-reconnect_at_eof", "1",
            "-timeout", "8000000",
            "-fflags", "+discardcorrupt", "-err_detect", "ignore_err",
            *header_args,
            "-i", url,
            "-vn", "-c:a", "aac", "-b:a", AUDIO_BITRATE,
            "-f", "segment", "-segment_time", str(SEGMENT_SEC),
            "-segment_atclocktime", "1", "-reset_timestamps", "1",
            "-strftime", "1",
            pattern,
            "-loglevel", "warning",
        ]
        logger.info(f"[{num:02d}] {name}: iniciando grabación")
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace")
            with active_lock:
                active_procs[num] = proc
            for line in proc.stderr:
                if stop_event.is_set() or _in_pause_window():
                    break
                line = line.strip()
                if line:
                    logger.warning(f"[{num:02d}] {name}: {line}")
            proc.wait(timeout=5)
        except Exception as e:
            logger.error(f"[{num:02d}] {name}: excepción — {e}")
        finally:
            with active_lock:
                active_procs.pop(num, None)
            try:
                if proc and proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
        if not stop_event.is_set():
            logger.warning(f"[{num:02d}] {name}: ffmpeg terminó, reintentando en {RESTART_DELAY}s")
            time.sleep(RESTART_DELAY)


def status_thread(stations: list[dict], threads: list[threading.Thread]):
    while not stop_event.wait(STATUS_EVERY):
        alive = sum(1 for t in threads if t.is_alive())
        logger.info(f"Estaciones grabando: {alive}/{len(stations)}")


def main():
    def _sigterm(signum, frame):
        logger.info("Señal de apagado recibida, deteniendo grabadores...")
        stop_event.set()

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    stations = list_radio_stations()
    logger.info(f"{len(stations)} estaciones de radio. Salida: {AUDIO_DIR.resolve()}. "
                f"Ventana nocturna sin grabar: {PAUSE_START_H:02d}:{PAUSE_START_M:02d}"
                f"-{PAUSE_END_H:02d}:{PAUSE_END_M:02d}")

    ps = threading.Thread(target=pause_scheduler, name="pause-scheduler", daemon=True)
    ps.start()

    threads = []
    for s in stations:
        t = threading.Thread(target=station_recorder, args=(s["num"], s["name"], s["url"], s["headers"]),
                              name=f"rec-{s['num']:02d}", daemon=True)
        t.start()
        threads.append(t)
        d = threading.Thread(target=drift_sampler, args=(s["num"], s["name"]),
                              name=f"drift-{s['num']:02d}", daemon=True)
        d.start()
        time.sleep(1)  # escalonar arranques

    st = threading.Thread(target=status_thread, args=(stations, threads), daemon=True)
    st.start()

    while not stop_event.is_set():
        time.sleep(1)

    for t in threads:
        t.join(timeout=10)


if __name__ == "__main__":
    main()
