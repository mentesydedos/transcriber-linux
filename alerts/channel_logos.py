"""
alerts/channel_logos.py — Logo de cada estación de radio para mostrarlo en
los resultados de búsqueda (ver _enrich_match en alerts/app.py), leyendo el
atributo tvg-logo="..." que ya trae "TV audio.m3u" para las estaciones de
Guadalajara (las únicas que de verdad se están grabando/transcribiendo hoy;
Radio CDMX.m3u y Radio Monterrey.m3u son investigación aparte, todavía no
conectada al pipeline de canales).

channel_name es el nombre EXACTO que usa manager.py/worker.py como nombre de
canal (viene del mismo #EXTINF), así que sirve tal cual como llave -- no hace
falta normalizar.
"""
import re
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
M3U_FILE = BASE_DIR / "TV audio.m3u"

_cache: dict[str, str] | None = None
_cache_mtime: float | None = None


def _load() -> dict[str, str]:
    logos = {}
    try:
        text = M3U_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return logos
    for line in text.splitlines():
        if not line.startswith("#EXTINF"):
            continue
        m_logo = re.search(r'tvg-logo="([^"]*)"', line)
        m_name = re.search(r',(.+)$', line)
        if m_logo and m_name:
            logos[m_name.group(1).strip()] = m_logo.group(1)
    return logos


def get_logo(channel_name: str | None) -> str | None:
    """None si el canal no tiene logo conocido (TV, o radio sin match en el
    M3U) -- la plantilla cae de vuelta al ícono genérico de "reproducir"."""
    global _cache, _cache_mtime
    if not channel_name:
        return None
    try:
        mtime = M3U_FILE.stat().st_mtime
    except OSError:
        mtime = None
    if _cache is None or mtime != _cache_mtime:
        _cache = _load()
        _cache_mtime = mtime
    return _cache.get(channel_name)
