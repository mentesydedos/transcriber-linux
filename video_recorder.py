"""
video_recorder.py — Graba en bloques de 30 min el video de los canales del M3U.

Jala el stream CRUDO de TVHeadend (`profile=pass`, sin transcodificar del lado
del servidor) y comprime LOCALMENTE: canales 1-NVENC_LIMIT con NVENC (GPU) a
AV1 ~600kb/s + AAC (hevc_nvenc hasta el 2026-08-10, cambiado a av1_nvenc por
mejor compresión -- mismo presupuesto de sesiones GPU, ver el plan de esa
fecha), el resto con libx264 (CPU) a ~1100kb/s + AAC (AV1/HEVC por CPU se
probaron y descartados para estos canales: sin margen suficiente de tiempo
real a 18 canales concurrentes, ver SVT-AV1 en el mismo plan). Esto le quita
toda la carga de
transcodificación a TVHeadend — su CPU no la soporta de forma confiable (ver
incidente 2026-08-04: con solo 4 transcodes concurrentes dejó de responder su
API). El GPU de esta máquina, en cambio, tiene margen de sobra (Parakeet ASR
usa 1-2%).

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
# El límite artificial de 8 sesiones NVENC de GeForce se eliminó con el
# parche de https://github.com/keylase/nvidia-patch (driver 580.173.02,
# aplicado 2026-08-06) — pero el techo real es la VRAM compartida con
# Parakeet/Cohere, y NO solo su uso en reposo (~7.3GB): CARGAR el modelo de
# Parakeet necesita un pico de VRAM más alto que su huella final, confirmado
# en producción (2026-08-06): con 14 sesiones NVENC + Cohere ya cargado,
# Parakeet falló 2 veces con CUDA out of memory al reiniciar (quedaba <40MB
# libres de 11.6GB). Se regresó a 8 (el original, antes del parche) porque
# es el único número ya validado como seguro para que AMBOS motores ASR
# puedan recargar su modelo sin chocar. Si se vuelve a subir, probar primero
# reiniciando Parakeet/Cohere con esa carga de NVENC activa, no solo medir
# el uso en reposo. Los canales por encima de este límite usan libx264 por
# CPU (i9-14900, 32 hilos, con margen de sobra).
NVENC_LIMIT    = int(os.environ.get("TRANSCRIBER_VIDEO_NVENC_LIMIT", "8"))
CPU_PRESET     = os.environ.get("TRANSCRIBER_VIDEO_CPU_PRESET", "veryfast")
# Bitrate CPU/libx264 (canales > NVENC_LIMIT): bajado de 1500k/1800k/3000k a
# 1100k/1300k/2200k (~27-39% menos según canal, medido) tras comparar cuadros
# reales lado a lado (texto de ticker, detalle fino) sin pérdida visible.
VIDEO_BITRATE  = os.environ.get("TRANSCRIBER_VIDEO_BITRATE", "1100k")
VIDEO_MAXRATE  = os.environ.get("TRANSCRIBER_VIDEO_MAXRATE", "1300k")
VIDEO_BUFSIZE  = os.environ.get("TRANSCRIBER_VIDEO_BUFSIZE", "2200k")
# AV1/NVENC (canales <= NVENC_LIMIT): cambiado de hevc_nvenc a av1_nvenc el
# 2026-08-10 -- esta GPU (RTX 4070, Ada Lovelace) sí tiene encoder de
# hardware AV1, probado con 10 sesiones simultáneas junto a las 8 de HEVC en
# vivo + los 2 modelos de IA cargados, sin fallar y liberando VRAM limpio
# (nvidia-patch ya quitó el límite artificial de sesiones). AV1 comprime
# mejor que HEVC a paridad de calidad, así que se bajó el bitrate objetivo
# (820k -> 600k) para capturar esa ganancia -- mismo presupuesto de sesiones
# GPU que antes (NVENC_LIMIT sin cambios), cero riesgo nuevo de VRAM.
# Verificar visualmente tras el rollout y ajustar si hace falta.
GPU_VIDEO_BITRATE = os.environ.get("TRANSCRIBER_VIDEO_GPU_BITRATE", "600k")
GPU_VIDEO_MAXRATE  = os.environ.get("TRANSCRIBER_VIDEO_GPU_MAXRATE", "720k")
GPU_VIDEO_BUFSIZE  = os.environ.get("TRANSCRIBER_VIDEO_GPU_BUFSIZE", "1200k")
VIDEO_HEIGHT   = os.environ.get("TRANSCRIBER_VIDEO_HEIGHT", "480")
AUDIO_BITRATE  = os.environ.get("TRANSCRIBER_VIDEO_ABITRATE", "96k")
RESTART_DELAY  = 5
STATUS_EVERY   = 60

# Ventana nocturna sin grabación (00:00-05:30 hora local por default) — libera
# ~23% del consumo diario de disco y deja un hueco fijo para mantenimiento
# (revisar servicios, limpiar, etc.) sin competir con grabación en curso.
PAUSE_START_H  = int(os.environ.get("TRANSCRIBER_VIDEO_PAUSE_START_H", "0"))
PAUSE_START_M  = int(os.environ.get("TRANSCRIBER_VIDEO_PAUSE_START_M", "0"))
PAUSE_END_H    = int(os.environ.get("TRANSCRIBER_VIDEO_PAUSE_END_H", "5"))
PAUSE_END_M    = int(os.environ.get("TRANSCRIBER_VIDEO_PAUSE_END_M", "30"))

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
    """Cuando entra la ventana nocturna, corta todas las grabaciones activas
    de inmediato (en vez de esperar a que cada una termine sola) — cada
    channel_recorder() detecta la ventana al reintentar y se queda dormido
    hasta la hora de reanudar."""
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
    # Canales GPU (AV1) graban en .mkv, no .ts -- probado el 2026-08-10:
    # MPEG-TS NO soporta AV1 de forma legible (lo muxa como "private data",
    # ffprobe lo lee como bin_data, no como video). Matroska sí soporta AV1
    # correctamente Y sigue siendo compatible con el `-sseof` que usa
    # videowall.py para leer el segmento activo en vivo (probado). Canales
    # CPU (H264) se quedan en .ts como siempre -- MPEG-TS lo soporta bien.
    ext = "mkv" if channel_id <= NVENC_LIMIT else "ts"
    pattern = str(folder / f"canal_{channel_id:02d}_{safe}_%Y-%m-%d_%H-%M.{ext}")
    raw_url = _raw_url(url)

    # scale=iw*sar:ih,setsar=1 corrige el SAR no-cuadrado de la fuente (704x480,
    # SAR 40:33, DAR real 16:9) antes de escalar a VIDEO_HEIGHT — si solo se
    # forzara un ancho fijo (ej. 720x480) la imagen saldría deformada.
    vf = (f"scale=trunc(iw*sar/2)*2:ih,setsar=1,"
          f"scale=-2:{VIDEO_HEIGHT}")

    if channel_id <= NVENC_LIMIT:
        vcodec_args = ["-c:v", "av1_nvenc", "-preset", "p4",
                       "-b:v", GPU_VIDEO_BITRATE, "-maxrate", GPU_VIDEO_MAXRATE, "-bufsize", GPU_VIDEO_BUFSIZE]
    else:
        vcodec_args = ["-c:v", "libx264", "-preset", CPU_PRESET,
                       "-b:v", VIDEO_BITRATE, "-maxrate", VIDEO_MAXRATE, "-bufsize", VIDEO_BUFSIZE]

    while not stop_event.is_set():
        if _in_pause_window():
            wait_s = _seconds_until_resume()
            logger.info(f"[{channel_id:02d}] {channel_name}: ventana nocturna, "
                        f"reanuda en {wait_s/60:.0f} min")
            stop_event.wait(wait_s)
            continue

        cmd = [
            "ffmpeg", "-nostdin",
            "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5", "-reconnect_at_eof", "1",
            "-timeout", "8000000",
            # +discardcorrupt / ignore_err: algunos subcanales (ej. A más +,
            # canal 11) transmiten AC3 mal formado en el origen -- confirmado
            # comparando contra un subcanal hermano en el mismo mux/tuner
            # (Azteca 7) que decodifica sin error, así que no es la señal ni
            # la red. Sin estas banderas ffmpeg llena el log con miles de
            # "Error submitting packet to decoder" por minuto; con ellas,
            # simplemente descarta el frame de audio dañado y sigue.
            "-fflags", "+discardcorrupt", "-err_detect", "ignore_err",
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
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace")
            with active_lock:
                active_procs[channel_id] = proc
            for line in proc.stderr:
                if stop_event.is_set() or _in_pause_window():
                    break
                line = line.strip()
                if line:
                    logger.warning(f"[{channel_id:02d}] {channel_name}: {line}")
            proc.wait(timeout=5)
        except Exception as e:
            logger.error(f"[{channel_id:02d}] {channel_name}: excepción — {e}")
        finally:
            with active_lock:
                active_procs.pop(channel_id, None)
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
                f"Salida: {VIDEO_DIR.resolve()}. "
                f"Ventana nocturna sin grabar: {PAUSE_START_H:02d}:{PAUSE_START_M:02d}"
                f"-{PAUSE_END_H:02d}:{PAUSE_END_M:02d}")

    ps = threading.Thread(target=pause_scheduler, name="pause-scheduler", daemon=True)
    ps.start()

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
