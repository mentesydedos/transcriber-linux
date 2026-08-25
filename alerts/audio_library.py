"""
alerts/audio_library.py — Audioteca: navegar grabaciones históricas de radio
por estación y bloque de 30 min, con reproductor de audio embebido y
descarga (misma idea que la Videoteca, alerts/library.py).

A diferencia del video, el disco local casi nunca tiene nada: radio_recorder.py
borra cada bloque local en cuanto backup_nas2_radio.py lo sube al NAS (ver
ambos scripts) -- el audio es solo un búfer de paso, no hay presión de disco
que justifique retenerlo local como con video. Por eso el NAS es la fuente
casi exclusiva aquí; local solo puede tener el bloque MÁS RECIENTE de cada
estación, todavía abierto/grabándose (nunca se borra ex-profeso mientras
está en uso).

Estructura NAS (igual que backup_nas2_radio.py):
    NAS_ROOT/YYYY-MM-DD/YYYY-MM-DD_HH-MM_HH-MM/canal_NN_Nombre_..._HH-MM.aac
"""
import os
import re
import time
from pathlib import Path
from datetime import datetime, timedelta

BASE_DIR  = Path(__file__).parent.parent
AUDIO_DIR = Path(os.environ.get("TRANSCRIBER_RADIO_DIR", str(BASE_DIR / "output_radio")))
# Mismo default que BACKUP_NAS2_RADIO_ROOT en backup_nas2_radio.py -- debe
# apuntar al mismo lugar donde ese script ya escribe.
NAS_AUDIO_ROOT = Path(os.environ.get("BACKUP_NAS2_RADIO_ROOT", "/mnt/nas2-tv/audio_radio"))

_SEG_RE = re.compile(r'_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})\.aac$')


def list_stations() -> list[dict]:
    """Estaciones de radio (num/name/url/headers) -- reusa radiowall.py, la
    misma fuente de verdad que usa el resto del sistema para esta lista."""
    from alerts.radiowall import list_radio_stations
    return list_radio_stations()


def get_station(num: int) -> dict | None:
    for s in list_stations():
        if s["num"] == num:
            return s
    return None


def _local_folder(num: int) -> Path | None:
    """Busca por glob en vez de reconstruir el nombre saneado -- evita
    duplicar la sanitización exacta de radio_recorder.py (_safe_name) y sus
    posibles casos borde."""
    if not AUDIO_DIR.is_dir():
        return None
    matches = list(AUDIO_DIR.glob(f"canal_{num:02d}_*"))
    return matches[0] if matches else None


def _nas_path_for(filename: str) -> Path | None:
    """Reconstruye la ruta NAS de un bloque a partir de su nombre de archivo
    -- misma lógica que _dest_for() en backup_nas2_radio.py (duplicada a
    propósito, igual que alerts/library.py con el video)."""
    m = _SEG_RE.search(filename)
    if not m:
        return None
    date_str, hh, mm = m.groups()
    start = datetime.strptime(f"{date_str} {hh}:{mm}", "%Y-%m-%d %H:%M")
    end = start + timedelta(minutes=30)
    block = f"{date_str}_{hh}-{mm}_{end.strftime('%H-%M')}"
    return NAS_AUDIO_ROOT / date_str / block / filename


_FILE_STATION_RE = re.compile(r'^canal_(\d+)_')
_NAS_INDEX_TTL = 300  # 5 min -- las fechas del NAS solo crecen (backup_nas2_radio.py
                      # nunca borra, cleanup_nas_radio.py solo borra pasados 30 días,
                      # un cambio lento), seguro cachear un rato.
_nas_index_cache: dict[int, set[str]] | None = None
_nas_index_cache_at: float = 0.0


def _nas_station_dates_index() -> dict[int, set[str]]:
    """{station_num: {fechas}} para TODO el NAS en un solo recorrido, en vez
    de recorrer las mismas carpetas de fecha una vez POR ESTACIÓN (mismo
    problema que tenía alerts/library.py con video -- 38 estaciones aquí,
    así que hubiera escalado todavía peor)."""
    global _nas_index_cache, _nas_index_cache_at
    now = time.time()
    if _nas_index_cache is not None and now - _nas_index_cache_at < _NAS_INDEX_TTL:
        return _nas_index_cache

    index: dict[int, set[str]] = {}
    if NAS_AUDIO_ROOT.is_dir():
        for date_dir in NAS_AUDIO_ROOT.iterdir():
            if not date_dir.is_dir() or not re.match(r'^\d{4}-\d{2}-\d{2}$', date_dir.name):
                continue
            for block_dir in date_dir.iterdir():
                if not block_dir.is_dir():
                    continue
                for f in block_dir.glob("canal_*.aac"):
                    m = _FILE_STATION_RE.match(f.name)
                    if m:
                        index.setdefault(int(m.group(1)), set()).add(date_dir.name)

    _nas_index_cache = index
    _nas_index_cache_at = now
    return index


def list_dates(num: int) -> list[str]:
    """Fechas (YYYY-MM-DD) con al menos un bloque grabado, local (el bloque
    en curso) o en el NAS, más reciente primero."""
    dates = set(_nas_station_dates_index().get(num, set()))
    folder = _local_folder(num)
    if folder is not None:
        for p in folder.glob("*.aac"):
            m = _SEG_RE.search(p.name)
            if m:
                dates.add(m.group(1))
    return sorted(dates, reverse=True)


def list_blocks(num: int, date: str) -> list[dict]:
    """Bloques de 30 min de `date` para la estación `num` -- local (el
    bloque en curso, si coincide con esa fecha) gana sobre NAS si el mismo
    horario existiera en ambos lados."""
    by_time: dict[str, Path] = {}

    date_dir = NAS_AUDIO_ROOT / date
    if date_dir.is_dir():
        prefix = f"canal_{num:02d}_"
        for p in date_dir.glob(f"*/{prefix}*.aac"):
            m = _SEG_RE.search(p.name)
            if not m:
                continue
            key = f"{m.group(2)}-{m.group(3)}"
            by_time[key] = p

    folder = _local_folder(num)
    if folder is not None:
        for p in folder.glob(f"*_{date}_*.aac"):
            m = _SEG_RE.search(p.name)
            if not m:
                continue
            key = f"{m.group(2)}-{m.group(3)}"
            by_time[key] = p  # local siempre gana -- es el bloque más fresco

    blocks = []
    for key in sorted(by_time):
        p = by_time[key]
        hh, mm = key.split("-")
        blocks.append({"file": p.name, "time": f"{hh}:{mm}"})
    return blocks


def resolve_block(num: int, filename: str) -> Path | None:
    """Ruta real del bloque `filename` -- local si todavía está ahí (el más
    reciente), si no en el NAS."""
    if not re.search(r'^[\w.-]+\.aac$', filename) or '/' in filename or '..' in filename:
        return None
    folder = _local_folder(num)
    if folder is not None:
        local = folder / filename
        if local.exists():
            return local
    nas_path = _nas_path_for(filename)
    if nas_path and nas_path.exists():
        return nas_path
    return None
