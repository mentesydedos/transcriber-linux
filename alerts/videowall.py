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
import io
import math
import re
import select
import subprocess
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterator

from PIL import Image, ImageDraw

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


_MJPEG_BOUNDARY = b"ffmpegframe"


def _kill_proc(proc: subprocess.Popen) -> None:
    """terminate + wait, kill de respaldo — mismo patrón que el fix de hoy en
    worker.py, para no dejar procesos huérfanos acumulándose."""
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _spawn_follow(segment: Path, fps: int, width: int) -> subprocess.Popen:
    # Sin -t: el proceso sigue corriendo indefinidamente leyendo el archivo
    # según video_recorder.py lo va escribiendo (confirmado: con -re, ffmpeg
    # tolera un archivo que crece y sigue produciendo frames en vez de cerrar
    # en el EOF momentáneo — es justo el comportamiento "tail -f" que
    # queremos aquí). -sseof -8 solo se usa al arrancar, para no decodificar
    # desde el inicio del segmento de 30 min.
    cmd = [
        "ffmpeg", "-re", "-sseof", "-8", "-i", str(segment),
        "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
        # fps antes que scale: se descartan los frames sobrantes ANTES de
        # reescalar, así solo se reescalan los frames que de verdad se usan.
        "-vf", f"fps={fps},scale={width}:-2", "-q:v", "4",
        "-f", "mjpeg", "-", "-loglevel", "error",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def _burst_frames(folder: Path, fps: int, width: int) -> Iterator[bytes]:
    """Yields frames JPEG crudos (sin envoltura multipart) de un proceso
    ffmpeg PERSISTENTE que sigue el archivo en crecimiento — a diferencia del
    diseño anterior (reiniciar ffmpeg cada pocos segundos), esto entrega
    video fluido de verdad: reiniciar el proceso cada pocos segundos causaba
    un micro-freeze perceptible en cada reinicio (confirmado: con reinicios
    cada 6s, cada canal se congelaba brevemente cada 6s).

    El único reinicio necesario es cuando el segmento rota cada 30 min
    (`_latest_segment` cambia) o si el proceso muere solo; ambos casos se
    revisan sin bloquear usando `select` con timeout de 0.5s en vez de un
    `read()` bloqueante — así el generador siempre puede reaccionar pronto
    tanto a la rotación como a un `.close()` externo (GeneratorExit al cerrar
    el cliente su conexión, o el thread productor deteniéndose)."""
    segment = _latest_segment(folder)
    while segment is None:
        time.sleep(1.0)
        segment = _latest_segment(folder)
    proc = _spawn_follow(segment, fps, width)
    buf = b""
    next_rotation_check = time.time() + 15
    try:
        while True:
            ready, _, _ = select.select([proc.stdout], [], [], 0.5)

            now = time.time()
            if now >= next_rotation_check:
                next_rotation_check = now + 15
                latest = _latest_segment(folder)
                if latest is not None and latest != segment:
                    _kill_proc(proc)
                    segment = latest
                    proc = _spawn_follow(segment, fps, width)
                    buf = b""
                    continue

            if not ready:
                if proc.poll() is not None:
                    # Murió solo (archivo borrado, error de ffmpeg, etc.) —
                    # re-resolver el segmento más reciente y reabrir.
                    segment = _latest_segment(folder) or segment
                    proc = _spawn_follow(segment, fps, width)
                    buf = b""
                continue

            chunk = proc.stdout.read(4096)
            if not chunk:
                # select() marcó el pipe listo pero no hay datos: es EOF real
                # (el proceso murió). BUG que se corrigió aquí: un simple
                # `continue` sin reabrir dejaba esto en loop infinito leyendo
                # EOF una y otra vez — el canal se quedaba congelado hasta el
                # siguiente chequeo de rotación (hasta 30 min). Reabrir de
                # inmediato, igual que en la rama `not ready` de arriba.
                segment = _latest_segment(folder) or segment
                proc = _spawn_follow(segment, fps, width)
                buf = b""
                continue
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                if start == -1:
                    buf = b""
                    break
                end = buf.find(b"\xff\xd9", start + 2)
                if end == -1:
                    buf = buf[start:]
                    break
                frame = buf[start:end + 2]
                buf = buf[end + 2:]
                yield frame
    finally:
        _kill_proc(proc)


def _spawn_follow_av(segment: Path, width: int) -> subprocess.Popen:
    """Como `_spawn_follow` pero codifica video+audio reales (h264+aac) en
    vez de frames MJPEG sueltos — solo tiene sentido para UN canal a la vez
    (la vista ampliada), no para los 26 del mosaico: aquí sí vale la pena un
    encode real, da mejor calidad por bit que MJPEG y trae audio."""
    cmd = [
        "ffmpeg", "-re", "-sseof", "-8", "-i", str(segment),
        "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", f"scale={width}:-2",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-b:v", "1200k",
        "-c:a", "aac", "-b:a", "96k", "-ac", "2",
        "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-frag_duration", "500000",
        "pipe:1", "-loglevel", "error",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def stream_av(folder: Path, width: int = 720) -> Iterator[bytes]:
    """Generador de bytes MP4 fragmentado (video+audio) de UN canal, para la
    vista ampliada — un `<video>` nativo del navegador lo consume
    directamente como stream progresivo. Mismo patrón de proceso persistente
    + `select` + rotación de segmento que `_burst_frames`, pero sin envoltura
    multipart: es un solo bytestream MP4 continuo."""
    segment = _latest_segment(folder)
    while segment is None:
        time.sleep(1.0)
        segment = _latest_segment(folder)
    proc = _spawn_follow_av(segment, width)
    next_rotation_check = time.time() + 15
    try:
        while True:
            ready, _, _ = select.select([proc.stdout], [], [], 0.5)

            now = time.time()
            if now >= next_rotation_check:
                next_rotation_check = now + 15
                latest = _latest_segment(folder)
                if latest is not None and latest != segment:
                    _kill_proc(proc)
                    segment = latest
                    proc = _spawn_follow_av(segment, width)
                    continue

            if not ready:
                if proc.poll() is not None:
                    segment = _latest_segment(folder) or segment
                    proc = _spawn_follow_av(segment, width)
                continue

            chunk = proc.stdout.read(65536)
            if not chunk:
                # EOF real (ver comentario en _burst_frames) — reabrir ya.
                segment = _latest_segment(folder) or segment
                proc = _spawn_follow_av(segment, width)
                continue
            yield chunk
    finally:
        _kill_proc(proc)


def stream_mjpeg(folder: Path, fps: int = 14, width: int = 320) -> Iterator[bytes]:
    """Generador multipart/x-mixed-replace de UN canal (endpoint
    `/videowall/stream/<num>`) — envoltura delgada sobre `_burst_frames`."""
    for frame in _burst_frames(folder, fps, width):
        yield (
            b"--" + _MJPEG_BOUNDARY + b"\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n" +
            frame + b"\r\n"
        )


def _produce(
    channel: dict,
    stop_event: threading.Event,
    shared: dict[int, bytes],
    lock: threading.Lock,
    fps: int,
    tile_w: int,
) -> None:
    """Corre en un thread daemon: alimenta `shared[num]` con el último frame
    JPEG disponible del canal. `gen.close()` en el finally dispara el
    terminate/wait/kill del ffmpeg persistente dentro de `_burst_frames` —
    misma limpieza que si fuera un stream HTTP normal."""
    gen = _burst_frames(channel["folder"], fps, tile_w)
    try:
        for frame in gen:
            if stop_event.is_set():
                break
            with lock:
                shared[channel["num"]] = frame
    except Exception:
        pass
    finally:
        gen.close()


def stream_wall_mjpeg(
    channels: list[dict],
    fps: int = 6,
    tile_w: int = 400,
    tile_h: int = 225,
    cols: int = 6,
    producer_fps: int = 6,
) -> Iterator[bytes]:
    """Generador multipart/x-mixed-replace de TODOS los canales compuestos en
    una sola grilla — un solo stream HTTP para toda la página, evitando el
    límite de 6 conexiones simultáneas por origen que tienen los navegadores
    (con un <img> por canal, los primeros 6 acaparan el cupo para siempre y
    el resto nunca conecta).

    Cada canal corre en su propio thread productor (`_produce`) con un
    ffmpeg persistente (ver `_burst_frames`); este generador solo lee el
    último frame de cada uno y compone. Al cerrar el cliente la conexión, se
    paran los threads y sus procesos ffmpeg."""
    stop_event = threading.Event()
    shared: dict[int, bytes] = {}
    lock = threading.Lock()
    threads = [
        threading.Thread(
            target=_produce, args=(c, stop_event, shared, lock, producer_fps, tile_w),
            daemon=True, name=f"videowall-produce-{c['num']}",
        )
        for c in channels
    ]
    for t in threads:
        t.start()

    rows = max(1, math.ceil(len(channels) / cols))
    canvas_w, canvas_h = cols * tile_w, rows * tile_h
    interval = 1.0 / fps

    try:
        while True:
            t0 = time.time()
            with lock:
                frames = dict(shared)
            canvas = Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0))
            draw = ImageDraw.Draw(canvas)
            for i, c in enumerate(channels):
                x, y = (i % cols) * tile_w, (i // cols) * tile_h
                data = frames.get(c["num"])
                if data:
                    try:
                        tile = Image.open(io.BytesIO(data)).convert("RGB").resize((tile_w, tile_h))
                        canvas.paste(tile, (x, y))
                    except Exception:
                        pass
                draw.text((x + 3, y + tile_h - 13), f"{c['num']:02d} {c['label']}"[:22], fill=(255, 255, 0))
            buf = io.BytesIO()
            canvas.save(buf, format="JPEG", quality=85)
            frame = buf.getvalue()
            yield (
                b"--" + _MJPEG_BOUNDARY + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n" +
                frame + b"\r\n"
            )
            dt = time.time() - t0
            if dt < interval:
                time.sleep(interval - dt)
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=6)
