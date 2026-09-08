"""
alerts/library.py — Videoteca: navegar grabaciones históricas de TV por
canal, hora (cada bloque son 30 min) o programa (título EPG del bloque),
con reproductor embebido en el dashboard y descarga.

Cada bloque vive primero como `.ts`/`.mkv` (video_recorder.py, mientras se
graba) y luego se remuxea a `.mp4` con faststart (finalize_video.py, una
vez cerrado, sin recodificar -- `-c copy`) -- ver el plan de "mejor
compresión + MP4 descargable" (2026-08-10). Este módulo reconoce ambas
extensiones: `.mp4` ya finalizado se sirve SIEMPRE directo (sin ffmpeg de
por medio, sin importar el canal/códec -- AV1 y H264 los decodifica el
navegador del cliente de forma nativa); `.ts`/`.mkv` (el bloque más
reciente, que el recorder puede seguir teniendo abierto) se remuxea al
vuelo (cacheado la primera vez) para obtener faststart, también sin
recodificar.

Solo TV: los canales de radio no graban audio de forma permanente (ver
worker.py/transcriber_parakeet.py — solo se transcribe y se descarta), así
que no hay nada que reproducir históricamente para radio todavía.

Disco local vs NAS: cleanup_video.py borra bloques locales cuando el disco
baja de su objetivo de espacio libre (ver ese script) -- con 26 canales
grabando, eso deja solo ~4-5 días de historial en disco. backup_nas2.py
respalda cada bloque .mp4 finalizado al NAS ANTES de que cleanup_video.py
pueda borrarlo (nunca borra local, puro respaldo), organizado por
fecha/bloque en vez de por canal:
    NAS_ROOT/YYYY-MM-DD/YYYY-MM-DD_HH-MM_HH-MM/canal_NN_Nombre_..._HH-MM.mp4
Este módulo combina ambas fuentes -- local primero (más rápido, NVMe) y NAS
como respaldo para fechas ya borradas localmente -- para que la videoteca
muestre todo el historial disponible, no solo lo que sigue en disco local.
"""
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

BASE_DIR  = Path(__file__).parent.parent
VIDEO_DIR = BASE_DIR / 'output_video'
CACHE_DIR = BASE_DIR / 'alerts' / 'cache' / 'library'
CACHE_DIR.mkdir(parents=True, exist_ok=True)
M3U_PATH  = BASE_DIR / 'TV audio.m3u'
# Mismo default que BACKUP_NAS2_ROOT en backup_nas2.py -- debe apuntar al
# mismo lugar donde ese script ya escribe.
NAS_VIDEO_ROOT = Path(os.environ.get("BACKUP_NAS2_ROOT", "/mnt/nas2-tv/videos 720x480"))

# Canales <= este número graban por GPU/NVENC (ver video_recorder.py,
# NVENC_LIMIT) en AV1 (ver el plan del 2026-08-10). AV1 tiene decodificación
# nativa en todos los navegadores modernos (Chrome/Firefox/Edge desde hace
# años, Safari 17+), así que YA NO se transcodifica a H264 para la vista
# previa -- ver get_or_build_clip(). Esta constante queda solo por si algún
# día se necesita distinguir el pipeline de grabación por canal.
GPU_CHANNEL_MAX = 8

_FOLDER_RE = re.compile(r'^canal_(\d+)_(.+)$')
_SEG_RE    = re.compile(r'_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})\.(?:ts|mp4|mkv)$')


def _m3u_names() -> dict[int, str]:
    """Nombres EXACTOS del M3U (1-indexado = channel_id) -- son los que
    coinciden con epg_programmes.channel_name (ver transcriber_parakeet.py:
    FileWindow usa este mismo nombre para _get_epg_programme). El nombre
    derivado de la carpeta (canal_04_N_ → "N ") no siempre coincide (p.ej.
    "N+" pierde el símbolo al sanitizar el nombre de carpeta).

    Parseo mínimo duplicado a propósito en vez de `import manager` -- mismo
    motivo que alerts/radiowall.py: no arrastrar manager.py a este proceso
    web (aunque hoy es liviano, no vale la pena depender de que lo siga
    siendo)."""
    names = {}
    try:
        with open(M3U_PATH, "r", encoding="utf-8", errors="replace") as f:
            i = 0
            for line in f:
                line = line.strip()
                if line.startswith("#EXTINF"):
                    m = re.search(r',(.+)$', line)
                    if m:
                        i += 1
                        names[i] = m.group(1).strip()
    except OSError:
        return {}
    return names


def list_channels() -> list[dict]:
    """Canales de TV con grabaciones disponibles en output_video/."""
    if not VIDEO_DIR.is_dir():
        return []
    names = _m3u_names()
    channels = []
    for folder in VIDEO_DIR.iterdir():
        if not folder.is_dir():
            continue
        m = _FOLDER_RE.match(folder.name)
        if not m:
            continue
        num = int(m.group(1))
        channels.append({
            "num": num,
            "folder": folder,
            "name": names.get(num, m.group(2).replace("_", " ").strip()),
        })
    channels.sort(key=lambda c: c["num"])
    return channels


def get_channel(num: int) -> dict | None:
    for c in list_channels():
        if c["num"] == num:
            return c
    return None


def overall_date_range() -> tuple[str, str] | None:
    """(fecha más antigua, fecha más reciente) con grabación en CUALQUIER
    canal -- para mostrar "hay datos desde X" en la portada de Videoteca,
    antes de entrar a un canal en particular (ver _nas_channel_dates_index,
    ya cachea el recorrido completo del NAS -- esto no agrega ningún
    escaneo nuevo, solo reduce el índice ya calculado)."""
    all_dates = set()
    for dates in _nas_channel_dates_index().values():
        all_dates |= dates
    if not all_dates:
        return None
    return min(all_dates), max(all_dates)


def _nas_path_for(filename: str) -> Path | None:
    """Reconstruye la ruta NAS de un bloque a partir de su nombre de archivo
    -- misma lógica que _dest_for() en backup_nas2.py (duplicada a propósito,
    igual que el resto de este módulo evita importar scripts standalone)."""
    m = _SEG_RE.search(filename)
    if not m:
        return None
    date_str, hh, mm = m.groups()
    from datetime import datetime, timedelta
    start = datetime.strptime(f"{date_str} {hh}:{mm}", "%Y-%m-%d %H:%M")
    end = start + timedelta(minutes=30)
    block = f"{date_str}_{hh}-{mm}_{end.strftime('%H-%M')}"
    return NAS_VIDEO_ROOT / date_str / block / filename


_FILE_CHANNEL_RE = re.compile(r'^canal_(\d+)_')
_NAS_INDEX_TTL = 300  # 5 min -- las fechas del NAS solo crecen (backup_nas2.py
                      # nunca borra), nunca desaparecen entre una consulta y
                      # la siguiente dentro de la ventana de cache, así que
                      # cachear es seguro.
# alerts.service corre con 8 workers de gunicorn (procesos separados, no
# hilos) -- un cache en una variable de módulo vive solo en el worker que
# lo llenó; los otros 7 seguían viendo cache=None y pagando el escaneo
# completo del NAS (~5s medido) cada vez que una petición les tocaba a
# ELLOS, sin importar que otro worker ya lo hubiera hecho. Por eso el cache
# vive en un archivo (compartido por los 8 procesos), no en memoria del
# proceso -- _nas_index_cache en memoria queda solo como espejo rápido
# dentro del mismo worker para no releer el archivo en cada petición.
_NAS_INDEX_CACHE_FILE = BASE_DIR / "alerts" / "cache" / "nas_video_index.json"
_NAS_INDEX_LOCK_FILE = BASE_DIR / "alerts" / "cache" / "nas_video_index.lock"
_nas_index_cache: dict[int, set[str]] | None = None
_nas_index_cache_at: float = 0.0
_nas_index_refreshing = threading.Lock()


def _scan_nas_channel_dates() -> dict[int, set[str]]:
    index: dict[int, set[str]] = {}
    if NAS_VIDEO_ROOT.is_dir():
        for date_dir in NAS_VIDEO_ROOT.iterdir():
            if not date_dir.is_dir() or not re.match(r'^\d{4}-\d{2}-\d{2}$', date_dir.name):
                continue
            for block_dir in date_dir.iterdir():
                if not block_dir.is_dir():
                    continue
                for f in block_dir.glob("canal_*.mp4"):
                    m = _FILE_CHANNEL_RE.match(f.name)
                    if m:
                        index.setdefault(int(m.group(1)), set()).add(date_dir.name)
    return index


def _write_index_file(index: dict[int, set[str]], at: float) -> None:
    _NAS_INDEX_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _NAS_INDEX_CACHE_FILE.with_suffix(".json.tmp")
    payload = {"cached_at": at, "index": {str(k): sorted(v) for k, v in index.items()}}
    tmp.write_text(json.dumps(payload))
    tmp.replace(_NAS_INDEX_CACHE_FILE)  # rename atómico -- otro worker nunca lee un archivo a medio escribir


def _read_index_file() -> tuple[dict[int, set[str]], float] | None:
    try:
        payload = json.loads(_NAS_INDEX_CACHE_FILE.read_text())
        index = {int(k): set(v) for k, v in payload["index"].items()}
        return index, payload["cached_at"]
    except (OSError, ValueError, KeyError):
        return None


def _refresh_nas_index_background():
    global _nas_index_cache, _nas_index_cache_at
    if not _nas_index_refreshing.acquire(blocking=False):
        return  # este worker ya está refrescando, no apilar otro hilo
    # Lock de archivo aparte (O_EXCL) -- evita que los OTROS 7 workers
    # disparen su propio escaneo completo al mismo tiempo cuando a todos
    # les toca una petición justo con el cache recién vencido.
    try:
        fd = os.open(str(_NAS_INDEX_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        _nas_index_refreshing.release()
        return
    try:
        index = _scan_nas_channel_dates()
        now = time.time()
        _write_index_file(index, now)
        _nas_index_cache = index
        _nas_index_cache_at = now
    finally:
        _NAS_INDEX_LOCK_FILE.unlink(missing_ok=True)
        _nas_index_refreshing.release()


def _nas_channel_dates_index() -> dict[int, set[str]]:
    """{channel_num: {fechas}} para TODO el NAS en un solo recorrido, en vez
    de recorrer las mismas carpetas de fecha una vez POR CANAL -- antes
    list_dates() escaneaba el NAS entero para un solo canal (~165ms medido
    con 18 días de historial), y la página de canal se volvía más lenta día
    a día conforme crecía el respaldo. Este índice se arma una vez y se
    reusa para los 26 canales.

    "Stale-while-revalidate": si ya hay algo en cache (aunque esté vencido),
    se devuelve de inmediato y el refresco se dispara en un hilo aparte --
    sin esto, cualquier visita después de que el cache de 5 min expirara
    (ej. entrar al día siguiente) pagaba el escaneo completo del NAS por
    red (~4.5s medido) de forma bloqueante. Como las fechas del NAS solo
    crecen, servir una copia de unos minutos de antigüedad mientras se
    actualiza en segundo plano es seguro -- a lo más falta el bloque más
    reciente hasta el siguiente refresco. Solo la primera consulta de
    todas (cache aún None, ej. recién reiniciado el servicio) espera al
    escaneo completo, porque no hay nada que servir todavía."""
    global _nas_index_cache, _nas_index_cache_at
    now = time.time()

    # Espejo en memoria de ESTE worker -- si ya lo leyó hace poco, ni
    # siquiera hace falta releer el archivo compartido.
    if _nas_index_cache is not None and now - _nas_index_cache_at < _NAS_INDEX_TTL:
        return _nas_index_cache

    from_file = _read_index_file()
    if from_file is not None:
        index, cached_at = from_file
        _nas_index_cache, _nas_index_cache_at = index, cached_at
        if now - cached_at >= _NAS_INDEX_TTL:
            threading.Thread(target=_refresh_nas_index_background, daemon=True).start()
        return index

    # Ni archivo compartido ni cache en memoria -- primera vez que CUALQUIER
    # worker pide esto desde que se reinició el servicio. Solo aquí se
    # bloquea a esperar el escaneo completo, porque no hay nada que servir.
    index = _scan_nas_channel_dates()
    _write_index_file(index, now)
    _nas_index_cache, _nas_index_cache_at = index, now
    return index


def list_dates(channel: dict) -> list[str]:
    """Fechas (YYYY-MM-DD) con al menos un bloque grabado, local o en el NAS
    (ver docstring del módulo), más reciente primero."""
    folder = channel["folder"]
    dates = set()
    for p in (*folder.glob("*.ts"), *folder.glob("*.mp4"), *folder.glob("*.mkv")):
        m = _SEG_RE.search(p.name)
        if m:
            dates.add(m.group(1))
    dates |= _nas_channel_dates_index().get(channel["num"], set())
    return sorted(dates, reverse=True)


def list_blocks(channel: dict, date: str, epg_db=None) -> list[dict]:
    """Bloques de 30 min de `date`, cada uno con el título del programa EPG
    (si hay datos) que estaba al aire al inicio del bloque -- esto es lo que
    permite filtrar/seleccionar "por programa" en el frontend. Prefiere el
    .mp4 ya finalizado sobre el .ts si por alguna razón existieran los dos
    (ventana breve mientras finalize_video.py hace el rename atómico), y
    local sobre NAS cuando el mismo bloque existe en ambos (más rápido de
    servir) -- el NAS solo llena los huecos de fechas ya borradas del disco
    local por cleanup_video.py."""
    from alerts.epg import get_programme_at
    folder = channel["folder"]
    by_time: dict[str, Path] = {}
    for p in (*folder.glob(f"*_{date}_*.ts"), *folder.glob(f"*_{date}_*.mp4"),
              *folder.glob(f"*_{date}_*.mkv")):
        m = _SEG_RE.search(p.name)
        if not m:
            continue
        key = f"{m.group(2)}-{m.group(3)}"
        if key not in by_time or p.suffix == ".mp4":
            by_time[key] = p

    date_dir = NAS_VIDEO_ROOT / date
    if date_dir.is_dir():
        prefix = f"canal_{channel['num']:02d}_"
        for p in date_dir.glob(f"*/{prefix}*.mp4"):
            m = _SEG_RE.search(p.name)
            if not m:
                continue
            key = f"{m.group(2)}-{m.group(3)}"
            if key not in by_time:  # local (de cualquier extensión) siempre gana
                by_time[key] = p

    blocks = []
    for key in sorted(by_time):
        p = by_time[key]
        hh, mm = key.split("-")
        title = ''
        if epg_db is not None:
            try:
                title = get_programme_at(epg_db, channel["name"], f"{date} {hh}:{mm}:00")
            except Exception:
                title = ''
        blocks.append({"file": p.name, "time": f"{hh}:{mm}", "title": title})
    return blocks


def _cache_path(channel_num: int, filename: str) -> Path:
    return CACHE_DIR / f"canal_{channel_num:02d}" / f"{filename}.mp4"


def get_or_build_clip(channel_num: int, folder: Path, filename: str,
                       timeout: int = 120) -> Path | None:
    """Devuelve un .mp4 reproducible en navegador del bloque `filename`.

    Camino rápido (sin ffmpeg): bloque ya finalizado a .mp4 por
    finalize_video.py -- se sirve el archivo tal cual (sin importar el
    canal/códec: H264 y AV1 los decodifica el navegador del cliente de
    forma nativa), faststart y codecs ya listos.

    Camino con remux (cacheado la primera vez, sin recodificar -- misma
    idea que finalize_video.py): bloque que todavía es .ts/.mkv (el más
    reciente, no finalizado todavía). Solo reempaqueta el contenedor para
    obtener faststart; el trabajo de decodificar lo sigue haciendo el
    navegador del cliente, no este servidor.

    Si el bloque ya no está en disco local (cleanup_video.py lo borró),
    se sirve directo desde el NAS -- ya está finalizado a .mp4 con
    faststart ahí también (backup_nas2.py solo copia bloques ya
    finalizados), así que tampoco necesita remux."""
    src = folder / filename
    if not src.exists():
        nas_src = _nas_path_for(filename)
        if nas_src and nas_src.exists():
            return nas_src
        return None

    if src.suffix == ".mp4":
        return src

    out = _cache_path(channel_num, filename)
    if out.exists() and out.stat().st_size > 0:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.mp4")

    def _run(with_audio: bool) -> bool:
        cmd = ["ffmpeg", "-y", "-i", str(src),
               "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
               "-map", "0:v:0"]
        if with_audio:
            # -c:a copy, no re-encode: video_recorder.py YA graba en AAC
            # (-c:a aac al capturar, ver video_recorder.py) sin importar el
            # códec de origen del canal -- re-codificar aquí era trabajo
            # desperdiciado (30 min de audio de más en cada bloque nuevo).
            cmd += ["-map", "0:a:0", "-c:v", "copy", "-c:a", "copy"]
        else:
            cmd += ["-c:v", "copy", "-an"]
        cmd += ["-movflags", "+faststart", str(tmp), "-loglevel", "error"]
        try:
            r = subprocess.run(cmd, timeout=timeout, stderr=subprocess.DEVNULL)
            return r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0
        except subprocess.TimeoutExpired:
            return False

    # Mapeo explícito de video+audio -- sin esto ffmpeg a veces solo toma el
    # video y se queda sin audio en la salida (visto en pruebas). Mismo -map
    # que ya usa videowall.py:_spawn_follow_av para estos mismos archivos.
    # Si el audio de origen está dañado (p.ej. "Canal 11", corrupción AC3 ya
    # documentada -- no es algo que se pueda arreglar aquí) el mux con audio
    # falla por completo; en ese caso se sirve el video solo, sin audio, en
    # vez de nada.
    if _run(with_audio=True) or _run(with_audio=False):
        tmp.replace(out)
        return out
    tmp.unlink(missing_ok=True)
    return None
