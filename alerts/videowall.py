"""
alerts/videowall.py — Miniaturas casi en vivo de los canales grabados, para el
monitor de señales del dashboard AlertaTV.

El MOSAICO (todos los canales a la vez) lee EXCLUSIVAMENTE los archivos
.ts/.mkv locales que video_recorder.py ya está escribiendo en output_video/
— nunca abre una conexión nueva a TVHeadend. La corrupción de video resuelta
antes (concurrencia de conexiones contra TVHeadend) es justo lo que el
mosaico debe evitar reintroducir: 26 conexiones en vivo simultáneas
reproducirían ese mismo riesgo.

Técnica del mosaico: `-sseof -8` busca por posición cercana al FINAL del
archivo (barato, no decodifica desde el inicio de un bloque de 30 min),
tomando el frame más reciente disponible del archivo que el recorder tiene
abierto ahora mismo. En `.mkv` (canales GPU/AV1) esto no es posible -- ver
`_seek_args()`.

La VISTA INDIVIDUAL (un canal a la vez, al hacer clic) SÍ conecta en vivo
directo a TVHeadend (`stream_live_av`) -- decisión explícita (2026-08-10):
el rezago de leer el archivo grabado no es aceptable para monitorear un
canal de cerca, y a diferencia del mosaico, nunca hay más de una conexión
nueva a la vez (una por cada pestaña que alguien tenga abierta viendo un
canal en detalle), un riesgo mucho más acotado que las 26 del mosaico.
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
M3U_PATH  = BASE_DIR / 'TV audio.m3u'

_FOLDER_RE = re.compile(r'^canal_(\d+)_(.+)$')

_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)


def _seek_args(segment: Path, back_sec: float = 8.0) -> list[str]:
    """Argumentos de ffmpeg para posicionarse cerca del final de `segment`
    mientras el recorder lo sigue escribiendo. Solo aplica a `.ts`
    (MPEG-TS): `-sseof` no necesita conocer la duración total para buscar
    desde el final, así que es barato y confiable ahí.

    `.mkv` (canales GPU/AV1, ver video_recorder.py) NO soporta ningún tipo
    de seek fiable mientras está abierto -- probado el 2026-08-10:
    `-sseof` falla directo ("Cannot use -sseof, file duration not known";
    ni siquiera ffprobe puede calcular la duración, confirmando que no hay
    índice/Cues todavía). Probé además calcular un offset por reloj y usar
    `-ss` normal (sin depender de la duración): funcionaba a veces pero
    fallaba sin avisar en otras (offsets grandes → "Output file is empty,
    nothing was encoded") -- Matroska sin índice a veces solo permite un
    escaneo lineal desde el inicio, no una búsqueda real, así que no es
    confiable. Por eso aquí se devuelve sin argumentos: `_spawn_follow`
    arranca desde el inicio del archivo con `-re`, sin reintentos ni
    reinicios (ver su docstring) -- estable aunque tarde en alcanzar lo
    actual si se abre a media grabación del bloque de 30 min."""
    if segment.suffix == '.ts':
        return ["-sseof", f"-{back_sec:g}"]
    return []


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
    """El .ts/.mkv con mayor mtime — el que el recorder tiene abierto ahora
    mismo. Por mtime y no por nombre: robusto ante reconexiones que generan
    nombres fuera de orden cronológico. .mkv: canales GPU/AV1 (ver
    video_recorder.py -- MPEG-TS no soporta AV1 de forma legible, así que
    esos canales graban en Matroska en vez de .ts; el seek en vivo contra
    .mkv necesita _seek_args(), ver su docstring). .ts: canales CPU/H264."""
    segments = [*folder.glob("*.ts"), *folder.glob("*.mkv")]
    if not segments:
        return None
    return max(segments, key=lambda p: p.stat().st_mtime)


def _extract_live_frame(path: Path, out_tmp: Path) -> bool:
    # -f mjpeg explícito: out_tmp es un archivo ".tmp" (para el rename atómico
    # en get_thumbnail), y ffmpeg no puede inferir el formato de salida de esa
    # extensión.
    cmd = [
        "ffmpeg", "-y", *_seek_args(path), "-i", str(path),
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
    # queremos aquí). El seek (_seek_args) solo se usa al arrancar, para no
    # decodificar desde el inicio del segmento de 30 min.
    cmd = [
        "ffmpeg", "-re", *_seek_args(segment), "-i", str(segment),
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


def _m3u_channel_url(num: int) -> tuple[str, str | None] | None:
    """URL (y header opcional, ver #EXTHEADER) del canal `num` (1-indexado)
    tal como aparece en el M3U -- misma fuente que usa video_recorder.py
    para grabar. Parseo mínimo duplicado a propósito, mismo motivo que
    alerts/radiowall.py y alerts/library.py: no importar manager.py."""
    try:
        with open(M3U_PATH, "r", encoding="utf-8", errors="replace") as f:
            i = 0
            headers = None
            for line in f:
                line = line.strip()
                if line.startswith("#EXTINF"):
                    i += 1
                    headers = None
                elif line.startswith("#EXTHEADER:"):
                    headers = line[len("#EXTHEADER:"):].strip()
                elif line and not line.startswith("#"):
                    if i == num:
                        return line, headers
    except OSError:
        return None
    return None


def _raw_url(url: str) -> str:
    """Mismo stream crudo (sin transcodificar en TVHeadend) que usa
    video_recorder.py -- la compresión la hace esta máquina, no TVHeadend."""
    return f"{url.split('?', 1)[0]}?profile=pass"


def _spawn_live_av(url: str, width: int | None) -> subprocess.Popen:
    """Como el antiguo `_spawn_follow_av` pero conectado EN VIVO a TVHeadend
    en vez de seguir un archivo local -- ver el docstring del módulo. Sin
    -re (la fuente ya es un stream en tiempo real, no hay que pausarse) ni
    seek (no hay archivo/segmento del que posicionarse).

    Sin downscale por default (`width=None`): la grabación 24/7 sí reduce a
    480p a propósito (26 canales simultáneos, ahorro de espacio), pero esta
    vista individual es UNA sola conexión bajo demanda, así que no hay razón
    para no dar la resolución completa que entregue cada canal.

    `-crf` (calidad constante) en vez de un `-b:v` fijo -- no todos los
    canales transmiten a la misma resolución (confirmado 2026-08-10: mezcla
    de 1080p y 480p). Con CRF, libx264 ya usa solo el bitrate que hace falta
    para la calidad pedida según resolución/complejidad real de cada canal,
    sin necesitar sondear la resolución de antemano (probé eso primero:
    agregaba una segunda conexión a TVHeadend solo para preguntar, sumando
    varios segundos más de espera antes de empezar a ver algo)."""
    vf = [] if not width else ["-vf", f"scale={width}:-2"]
    cmd = [
        "ffmpeg", "-nostdin",
        "-reconnect", "1", "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5", "-reconnect_at_eof", "1", "-timeout", "8000000",
        "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
        "-i", _raw_url(url),
        "-map", "0:v:0", "-map", "0:a:0?",
        *vf,
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-crf", "20", "-maxrate", "6000k", "-bufsize", "10000k",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-frag_duration", "500000",
        "pipe:1", "-loglevel", "error",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def stream_live_av(num: int, width: int | None = None) -> Iterator[bytes]:
    """Generador de bytes MP4 fragmentado (video+audio) EN VIVO de UN canal,
    conectado directo a TVHeadend -- para la vista ampliada (clic en un
    canal del mosaico). A diferencia del mosaico (26 conexiones sería el
    mismo riesgo que causó el incidente de corrupción ya resuelto antes),
    esto nunca abre más de una conexión nueva a la vez por pestaña abierta.
    Un `<video>` nativo del navegador lo consume directamente como stream
    progresivo."""
    found = _m3u_channel_url(num)
    if found is None:
        return
    url, _headers = found
    proc = _spawn_live_av(url, width)
    try:
        while True:
            ready, _, _ = select.select([proc.stdout], [], [], 0.5)
            if not ready:
                if proc.poll() is not None:
                    proc = _spawn_live_av(url, width)
                continue
            chunk = proc.stdout.read(65536)
            if not chunk:
                proc = _spawn_live_av(url, width)
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
