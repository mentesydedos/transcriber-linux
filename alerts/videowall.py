"""
alerts/videowall.py — Miniaturas casi en vivo de los canales grabados, para el
monitor de señales del dashboard AlertaTV.

IMPORTANTE: esto lee EXCLUSIVAMENTE los archivos .ts locales que
video_recorder.py ya está escribiendo en output_video/ — nunca abre una
conexión nueva a TVHeadend. La corrupción de video resuelta hoy (concurrencia
de conexiones contra TVHeadend) es justo lo que este módulo debe evitar
reintroducir.

Técnica: `-sseof -8` busca por posición cercana al FINAL del archivo (barato,
no decodifica desde el inicio de un .ts de 30 min), tomando el frame más
reciente disponible del archivo que el recorder tiene abierto ahora mismo.
"""
import re
import subprocess
import threading
import time
from collections import defaultdict
from pathlib import Path

BASE_DIR  = Path(__file__).parent.parent
VIDEO_DIR = BASE_DIR / 'output_video'
CACHE_DIR = BASE_DIR / 'alerts' / 'cache' / 'videowall'
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_FOLDER_RE = re.compile(r'^canal_(\d+)_(.+)$')

_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)


def list_channels() -> list[dict]:
    """Escanea output_video/ — fuente de verdad de qué se está grabando ahora
    mismo (no re-parsear el M3U, que puede no coincidir con lo realmente
    activo)."""
    if not VIDEO_DIR.is_dir():
        return []
    channels = []
    for folder in VIDEO_DIR.iterdir():
        if not folder.is_dir():
            continue
        m = _FOLDER_RE.match(folder.name)
        if not m:
            continue
        channels.append({
            "num": int(m.group(1)),
            "folder": folder,
            "label": m.group(2).replace("_", " ").strip(),
        })
    channels.sort(key=lambda c: c["num"])
    return channels


def _latest_segment(folder: Path) -> Path | None:
    """El .ts con mayor mtime — el que el recorder tiene abierto ahora mismo.
    Por mtime y no por nombre: robusto ante reconexiones que generan nombres
    fuera de orden cronológico."""
    segments = list(folder.glob("*.ts"))
    if not segments:
        return None
    return max(segments, key=lambda p: p.stat().st_mtime)


def _extract_live_frame(path: Path, out_tmp: Path) -> bool:
    # -f mjpeg explícito: out_tmp es un archivo ".tmp" (para el rename atómico
    # en get_thumbnail), y ffmpeg no puede inferir el formato de salida de esa
    # extensión.
    cmd = [
        "ffmpeg", "-y", "-sseof", "-8", "-i", str(path),
        "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
        "-update", "1", "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5",
        "-f", "mjpeg", str(out_tmp), "-loglevel", "error",
    ]
    try:
        r = subprocess.run(cmd, timeout=6)
        if r.returncode == 0 and out_tmp.exists() and out_tmp.stat().st_size > 0:
            return True
    except (subprocess.TimeoutExpired, OSError):
        pass
    # Fallback: archivo recién rotado (<8s de contenido) — decodificar desde
    # el inicio es barato porque en ese caso el archivo es chico.
    cmd_fallback = [
        "ffmpeg", "-y", "-i", str(path),
        "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
        "-update", "1", "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5",
        "-f", "mjpeg", str(out_tmp), "-loglevel", "error",
    ]
    try:
        r = subprocess.run(cmd_fallback, timeout=6)
        return r.returncode == 0 and out_tmp.exists() and out_tmp.stat().st_size > 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def get_thumbnail(num: int, folder: Path, ttl: float = 5.0) -> Path | None:
    """Miniatura cacheada del canal `num`. Regenera si tiene más de `ttl`
    segundos, con lock por canal para no duplicar trabajo entre pestañas/
    requests simultáneas — el que pierde el lock sirve lo que ya había
    (fresco o levemente stale) en vez de esperar."""
    out = CACHE_DIR / f"canal_{num:02d}.jpg"
    if out.exists() and (time.time() - out.stat().st_mtime) < ttl:
        return out

    lock = _locks[num]
    if not lock.acquire(blocking=False):
        return out if out.exists() else None

    try:
        # Re-chequear bajo lock: otro thread pudo haber regenerado ya.
        if out.exists() and (time.time() - out.stat().st_mtime) < ttl:
            return out
        segment = _latest_segment(folder)
        if segment is None:
            return out if out.exists() else None
        tmp = out.with_suffix(".tmp")
        if _extract_live_frame(segment, tmp):
            tmp.replace(out)  # rename atómico: nunca se sirve un jpg a medias
            return out
        return out if out.exists() else None
    finally:
        lock.release()
