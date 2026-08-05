"""
video_recorder.py — Graba en bloques de 30 min el video de los canales del M3U.

Jala el stream CRUDO de TVHeadend (`profile=pass`, sin transcodificar del lado
del servidor) y comprime LOCALMENTE con NVENC (GPU) a H.264 ~1500kb/s + AAC.
Esto le quita toda la carga de transcodificación a TVHeadend — su CPU no la
soporta de forma confiable (ver incidente 2026-08-04: con solo 4 transcodes
concurrentes dejó de responder su API). El GPU de esta máquina, en cambio,
tiene margen de sobra (Parakeet ASR usa 1-2%).

Limitado a TRANSCRIBER_VIDEO_MAX_CHANNELS canales (default 19) mientras el
enlace de red siga en 100Mb/s: crudo ≈ 4.5Mbps/canal, y 19 canales + el audio
de transcripción (~10.6Mbps) caben con margen. Subir el límite cuando el
enlace pase a Gigabit.

Un proceso ffmpeg por canal, supervisado (se reinicia si muere). La rotación
de bloques la hace el propio ffmpeg (-f segment), alineada a reloj (:00/:30).
"""
import os
import re
import sys
import time
import signal
import logging
import subprocess
import threading
from pathlib import Path

M3U_FILE       = os.environ.get("TRANSCRIBER_M3U", "TV audio.m3u")
VIDEO_DIR      = Path(os.environ.get("TRANSCRIBER_VIDEO_DIR", "output_video"))
SEGMENT_SEC    = int(os.environ.get("TRANSCRIBER_VIDEO_SEGMENT_SEC", "1800"))
MAX_CHANNELS   = int(os.environ.get("TRANSCRIBER_VIDEO_MAX_CHANNELS", "19"))
# El driver de la RTX 4070 limita NVENC a 8 sesiones concurrentes en GeForce
# (probado 2026-08-04: canales 9+ fallan con "OpenEncodeSessionEx failed").
# Los canales por encima de este limite usan libx264 por CPU (i9-14900, 32
# hilos, con margen de sobra).
NVENC_LIMIT    = int(os.environ.get("TRANSCRIBER_VIDEO_NVENC_LIMIT", "8"))
CPU_PRESET     = os.environ.get("TRANSCRIBER_VIDEO_CPU_PRESET", "veryfast")
VIDEO_BITRATE  = os.environ.get("TRANSCRIBER_VIDEO_BITRATE", "1500k")
VIDEO_MAXRATE  = os.environ.get("TRANSCRIBER_VIDEO_MAXRATE", "1800k")
VIDEO_BUFSIZE  = os.environ.get("TRANSCRIBER_VIDEO_BUFSIZE", "3000k")
VIDEO_HEIGHT   = os.environ.get("TRANSCRIBER_VIDEO_HEIGHT", "480")
AUDIO_BITRATE  = os.environ.get("TRANSCRIBER_VIDEO_ABITRATE", "96k")
RESTART_DELAY  = 5
STATUS_EVERY   = 60

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
VIDEO_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "video_recorder.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("video_recorder")

stop_event = threading.Event()


def parse_m3u(filepath: str) -> list[dict]:
    channels, current_name = [], None
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line == "#EXTM3U":
                continue
            if line.startswith("#EXTINF"):
                m = re.search(r",(.+)$", line)
                current_name = m.group(1).strip() if m else f"Canal_{len(channels)+1}"
            elif not line.startswith("#"):
                channels.append({"name": current_name or f"Canal_{len(channels)+1}", "url": line})
                current_name = None
    return channels


def _raw_url(audio_url: str) -> str:
    """Mismo host/credenciales/channelid que el M3U de audio, pidiendo el
    stream crudo (sin transcodificar) — la compresión la hace esta máquina."""
    base = audio_url.split("?", 1)[0]
    return f"{base}?profile=pass"


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)


def channel_recorder(channel_id: int, channel_name: str, url: str):
    safe = _safe_name(channel_name)
    folder = VIDEO_DIR / f"canal_{channel_id:02d}_{safe}"
    folder.mkdir(parents=True, exist_ok=True)
    pattern = str(folder / f"canal_{channel_id:02d}_{safe}_%Y-%m-%d_%H-%M.ts")
    raw_url = _raw_url(url)

    # scale=iw*sar:ih,setsar=1 corrige el SAR no-cuadrado de la fuente (704x480,
    # SAR 40:33, DAR real 16:9) antes de escalar a VIDEO_HEIGHT — si solo se
    # forzara un ancho fijo (ej. 720x480) la imagen saldría deformada.
    vf = (f"scale=trunc(iw*sar/2)*2:ih,setsar=1,"
          f"scale=-2:{VIDEO_HEIGHT}")

    if channel_id <= NVENC_LIMIT:
        vcodec_args = ["-c:v", "h264_nvenc", "-preset", "p4",
                       "-b:v", VIDEO_BITRATE, "-maxrate", VIDEO_MAXRATE, "-bufsize", VIDEO_BUFSIZE]
    else:
        vcodec_args = ["-c:v", "libx264", "-preset", CPU_PRESET,
                       "-b:v", VIDEO_BITRATE, "-maxrate", VIDEO_MAXRATE, "-bufsize", VIDEO_BUFSIZE]

    while not stop_event.is_set():
        cmd = [
            "ffmpeg", "-nostdin",
            "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5", "-reconnect_at_eof", "1",
            "-timeout", "8000000",
            "-i", raw_url,
            "-vf", vf,
            *vcodec_args,
            "-c:a", "aac", "-b:a", AUDIO_BITRATE,
            "-f", "segment", "-segment_time", str(SEGMENT_SEC),
            "-segment_atclocktime", "1", "-reset_timestamps", "1",
            "-strftime", "1",
            pattern,
            "-loglevel", "warning",
        ]
        logger.info(f"[{channel_id:02d}] {channel_name}: iniciando grabación")
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace")
            for line in proc.stderr:
                if stop_event.is_set():
                    break
                line = line.strip()
                if line:
                    logger.warning(f"[{channel_id:02d}] {channel_name}: {line}")
            proc.wait(timeout=5)
        except Exception as e:
            logger.error(f"[{channel_id:02d}] {channel_name}: excepción — {e}")
        finally:
            try:
                if proc and proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
        if not stop_event.is_set():
            logger.warning(f"[{channel_id:02d}] {channel_name}: ffmpeg terminó, "
                            f"reintentando en {RESTART_DELAY}s")
            time.sleep(RESTART_DELAY)


def status_thread(channels: list[dict], threads: list[threading.Thread]):
    while not stop_event.wait(STATUS_EVERY):
        alive = sum(1 for t in threads if t.is_alive())
        logger.info(f"Canales grabando: {alive}/{len(channels)}")


def main():
    def _sigterm(signum, frame):
        logger.info("Señal de apagado recibida, deteniendo grabadores...")
        stop_event.set()

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    all_channels = parse_m3u(M3U_FILE)
    channels = all_channels[:MAX_CHANNELS]
    deferred = all_channels[MAX_CHANNELS:]
    logger.info(f"{len(all_channels)} canales en {M3U_FILE}. Grabando {len(channels)} "
                f"(limite TRANSCRIBER_VIDEO_MAX_CHANNELS={MAX_CHANNELS}). "
                f"Diferidos por ancho de banda: {[c['name'] for c in deferred] or 'ninguno'}. "
                f"Salida: {VIDEO_DIR.resolve()}")

    threads = []
    for i, ch in enumerate(channels, start=1):
        t = threading.Thread(target=channel_recorder, args=(i, ch["name"], ch["url"]),
                              name=f"rec-{i:02d}", daemon=True)
        t.start()
        threads.append(t)
        time.sleep(1)  # escalonar arranques

    st = threading.Thread(target=status_thread, args=(channels, threads), daemon=True)
    st.start()

    while not stop_event.is_set():
        time.sleep(1)

    for t in threads:
        t.join(timeout=10)


if __name__ == "__main__":
    main()
