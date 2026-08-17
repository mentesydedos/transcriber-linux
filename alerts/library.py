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
"""
import re
import subprocess
from pathlib import Path

BASE_DIR  = Path(__file__).parent.parent
VIDEO_DIR = BASE_DIR / 'output_video'
CACHE_DIR = BASE_DIR / 'alerts' / 'cache' / 'library'
CACHE_DIR.mkdir(parents=True, exist_ok=True)
M3U_PATH  = BASE_DIR / 'TV audio.m3u'

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


def list_dates(folder: Path) -> list[str]:
    """Fechas (YYYY-MM-DD) con al menos un bloque grabado, más reciente primero."""
    dates = set()
    for p in (*folder.glob("*.ts"), *folder.glob("*.mp4"), *folder.glob("*.mkv")):
        m = _SEG_RE.search(p.name)
        if m:
            dates.add(m.group(1))
    return sorted(dates, reverse=True)


def list_blocks(channel: dict, date: str, epg_db=None) -> list[dict]:
    """Bloques de 30 min de `date`, cada uno con el título del programa EPG
    (si hay datos) que estaba al aire al inicio del bloque -- esto es lo que
    permite filtrar/seleccionar "por programa" en el frontend. Prefiere el
    .mp4 ya finalizado sobre el .ts si por alguna razón existieran los dos
    (ventana breve mientras finalize_video.py hace el rename atómico)."""
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
    navegador del cliente, no este servidor."""
    src = folder / filename
    if not src.exists():
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
