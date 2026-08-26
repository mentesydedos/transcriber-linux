"""
alerts/transcript_archive.py — Acervo de Transcripciones: navegar TODO lo
transcrito (TV + radio) por canal y fecha, en orden cronológico, con link al
bloque de video/audio correspondiente en la Videoteca/Audioteca cuando
existe (mismo patrón de esas dos, pero mostrando el texto en vez de un
reproductor como contenido principal).

A diferencia de "Búsquedas" (alerts/watcher.py), esto no filtra por palabra
clave -- es el registro completo, útil para leer/repasar lo que se dijo en
un canal en una fecha, sin tener que armar una búsqueda primero.
"""
import sqlite3
import time
from pathlib import Path
from datetime import datetime, timedelta

from alerts.channel_types import channel_type

BASE_DIR = Path(__file__).parent.parent
TRANS_DB = BASE_DIR / 'transcriptions.db'

# list_channels() hace COUNT(*)/MAX(timestamp)/MAX(channel_name) agrupado
# sobre TODA la tabla (4.8M+ filas y creciendo) -- MAX(channel_name) en
# particular no puede aprovechar ningún índice, así que cada llamada
# recorre la tabla entera (~9s medido). Se cachea unos minutos -- es la
# portada de navegación, no necesita reflejar el segundo exacto.
_CHANNELS_CACHE_TTL = 180
_channels_cache: list[dict] | None = None
_channels_cache_at: float = 0.0


def _tdb() -> sqlite3.Connection:
    conn = sqlite3.connect(str(TRANS_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def list_channels() -> list[dict]:
    """Todos los canales (TV + radio) con al menos una transcripción --
    MAX(channel_name) es una simplificación segura (el nombre es estable
    por canal en la práctica; evita un self-join solo para el caso raro de
    que cambiara)."""
    global _channels_cache, _channels_cache_at
    now = time.time()
    if _channels_cache is not None and now - _channels_cache_at < _CHANNELS_CACHE_TTL:
        return _channels_cache

    conn = _tdb()
    rows = conn.execute("""
        SELECT channel_id, MAX(channel_name) as channel_name,
               COUNT(*) as n, MAX(timestamp) as last_ts
        FROM transcriptions
        WHERE channel_id IS NOT NULL
        GROUP BY channel_id
        ORDER BY channel_id
    """).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            "channel_id": r["channel_id"],
            "channel_name": r["channel_name"],
            "kind": channel_type(r["channel_id"]),
            "count": r["n"],
            "last_ts": r["last_ts"],
        })
    _channels_cache = out
    _channels_cache_at = now
    return out


def get_channel_name(channel_id: int) -> str | None:
    conn = _tdb()
    row = conn.execute(
        "SELECT channel_name FROM transcriptions WHERE channel_id=? ORDER BY timestamp DESC LIMIT 1",
        (channel_id,)
    ).fetchone()
    conn.close()
    return row["channel_name"] if row else None


def list_dates(channel_id: int) -> list[str]:
    """Fechas (YYYY-MM-DD) con al menos una transcripción, más reciente
    primero -- usa idx_trans_channel_ts (channel_id, timestamp)."""
    conn = _tdb()
    rows = conn.execute("""
        SELECT DISTINCT date(timestamp) as d FROM transcriptions
        WHERE channel_id=? ORDER BY d DESC
    """, (channel_id,)).fetchall()
    conn.close()
    return [r["d"] for r in rows]


def list_chunks(channel_id: int, date: str) -> list[dict]:
    """Fragmentos transcritos de `date` en orden cronológico, con la URL del
    bloque de 30 min correspondiente en Videoteca/Audioteca (si aplica).
    Rango de timestamp en vez de date(timestamp)=? -- la expresión no puede
    usar idx_trans_channel_ts (channel_id, timestamp), así que evaluaba
    date() en TODAS las filas históricas del canal para encontrar las de un
    solo día; el rango sí acota el índice directo a esas filas."""
    next_date = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    conn = _tdb()
    rows = conn.execute("""
        SELECT timestamp, text, has_music FROM transcriptions
        WHERE channel_id=? AND timestamp >= ? AND timestamp < ?
        ORDER BY timestamp ASC
    """, (channel_id, date, next_date)).fetchall()
    conn.close()

    kind = channel_type(channel_id)
    # Cache por bloque de 30 min (no por fragmento) -- muchos fragmentos
    # consecutivos comparten el mismo bloque, y checar existencia en el NAS
    # (CIFS) para cada uno de los ~60 fragmentos/bloque sería lento sin
    # necesidad. Ver retención real: Videoteca solo tiene NAS desde el
    # 2026-08-08 y disco local ~4-5 días, así que fechas viejas del acervo
    # (transcripciones se guardan mucho más tiempo) legítimamente no tienen
    # clip -- por eso se verifica existencia antes de ofrecer el link.
    block_cache: dict[str, str | None] = {}
    chunks = []
    for r in rows:
        text = r["text"]
        if not text or text == '[~]':
            continue
        # HH:MM con MM aplastado a 00/30 -- clave estable del bloque de 30
        # min al que pertenece este fragmento, sin volver a parsear con
        # datetime solo para cachear.
        hh, mm = r["timestamp"][11:13], r["timestamp"][14:16]
        block_key = f"{hh}:{'00' if mm < '30' else '30'}"
        if block_key not in block_cache:
            block_cache[block_key] = _clip_url(channel_id, kind, r["timestamp"])
        chunks.append({
            "timestamp": r["timestamp"],
            "text": text,
            "clip_url": block_cache[block_key],
            "has_music": bool(r["has_music"]) if "has_music" in r.keys() else False,
        })
    return chunks


def _clip_url(channel_id: int, kind: str, timestamp: str) -> str | None:
    """URL directa al bloque de 30 min (Videoteca/Audioteca) que contiene
    este momento -- floor a :00/:30, misma convención que video_recorder.py/
    radio_recorder.py. None si el archivo ya no existe ni local ni en NAS
    (fechas fuera de la retención de cada uno) -- mejor no ofrecer un link
    roto que confiar en que get_or_build_clip/resolve_block ya lo filtran."""
    try:
        ts = datetime.strptime(timestamp[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    block_min = 0 if ts.minute < 30 else 30
    block = ts.replace(minute=block_min, second=0, microsecond=0)
    date_str = block.strftime("%Y-%m-%d")
    hh_mm = block.strftime("%H-%M")

    if kind == "tv":
        from alerts.library import get_channel, _nas_path_for
        ch = get_channel(channel_id)
        if ch is None:
            return None
        filename = f"{ch['folder'].name}_{date_str}_{hh_mm}.mp4"
        local = ch['folder'] / filename
        if not local.exists():
            nas = _nas_path_for(filename)
            if not (nas and nas.exists()):
                return None
        return f"/library/video/{channel_id}/{filename}"
    elif kind == "radio":
        from alerts.audio_library import _local_folder, resolve_block
        folder = _local_folder(channel_id)
        if folder is None:
            return None
        filename = f"{folder.name}_{date_str}_{hh_mm}.aac"
        if resolve_block(channel_id, filename) is None:
            return None
        return f"/audio-library/play/{channel_id}/{filename}"
    return None
