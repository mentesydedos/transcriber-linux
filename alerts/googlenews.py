"""
alerts/googlenews.py — Búsqueda de noticias en Google Noticias (RSS público,
sin API key) para complementar las coincidencias de TV/radio con prensa
escrita/digital. Cada artículo se guarda como un match más (ver
alerts/channel_types.py NEWS_CHANNEL_ID) para reusar toda la UI existente
(tabla de coincidencias, resaltado, reportes, similitudes).

Fuente: https://news.google.com/rss/search?q=... -- feed RSS público que ya
hace la búsqueda por keyword del lado de Google (no hace falta re-implementar
el matching fonético/exacto que sí se necesita para las transcripciones).
Soporta acotar por fecha con los operadores "after:"/"before:" dentro del
query -- se usa para el fetch histórico inicial (rango date_start..date_end
de la búsqueda); el polling en vivo (alerts/watcher.py) llama sin acotar,
confiando en el índice único de matches para no duplicar artículos ya vistos.

Nota: es un endpoint público no documentado oficialmente (sin API key ni
SLA) -- si Google cambia el formato, fetch_articles() simplemente empieza a
devolver listas vacías (logueado como error), sin tumbar el resto del
watcher.
"""
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

logger = logging.getLogger('googlenews')

RSS_BASE = 'https://news.google.com/rss/search'
TIMEOUT  = 15
HL, GL   = 'es-419', 'MX'   # español latam / México, igual que el resto del pipeline


def _build_url(query: str, date_from: str | None, date_to: str | None) -> str:
    q = query
    if date_from:
        q += f' after:{date_from}'
    if date_to:
        # El operador "before:" de Google excluye el día indicado por
        # completo (before:2026-08-14 no trae nada DE ese día, solo lo
        # anterior) -- date_to acá es inclusivo (misma convención que
        # date_start/date_end en el resto de la app), así que hay que
        # correr el límite un día para no perder justo el día final --
        # el caso más común siendo date_start == date_end == hoy.
        before = (datetime.strptime(date_to, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
        q += f' before:{before}'
    params = {'q': q, 'hl': HL, 'gl': GL, 'ceid': f'{GL}:{HL}'}
    return f'{RSS_BASE}?{urllib.parse.urlencode(params)}'


def _clean_title(raw_title: str, source: str) -> str:
    """Google Noticias pone '<título> - <fuente>' en <title> -- se quita el
    sufijo de fuente (ya viene aparte en <source>) para no duplicarlo."""
    suffix = f' - {source}'
    if source and raw_title.endswith(suffix):
        return raw_title[:-len(suffix)]
    return raw_title


def fetch_articles(query: str, date_from: str | None = None, date_to: str | None = None,
                    limit: int = 50) -> list[dict]:
    """Devuelve [{title, link, source, published}] para una keyword.
    date_from/date_to en formato 'YYYY-MM-DD' (opcional, acota con
    after:/before:). published es datetime naive en hora local."""
    url = _build_url(query, date_from, date_to)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
    except urllib.error.URLError as e:
        logger.error(f"Google Noticias: error consultando '{query}': {e}")
        return []
    except Exception as e:
        logger.error(f"Google Noticias: error inesperado consultando '{query}': {e}")
        return []

    try:
        root = ET.fromstring(raw)
    except Exception as e:
        logger.error(f"Google Noticias: error parseando XML para '{query}': {e}")
        return []

    out = []
    for item in root.findall('.//item')[:limit]:
        raw_title = (item.findtext('title') or '').strip()
        link      = (item.findtext('link') or '').strip()
        source_el = item.find('source')
        source    = (source_el.text or '').strip() if source_el is not None else ''
        pub_raw   = item.findtext('pubDate') or ''
        if not raw_title or not link or not pub_raw:
            continue
        try:
            pub_dt = parsedate_to_datetime(pub_raw)
            if pub_dt.tzinfo is not None:
                pub_dt = pub_dt.astimezone().replace(tzinfo=None)
        except Exception:
            continue
        out.append({
            'title':     _clean_title(raw_title, source),
            'link':      link,
            'source':    source or 'Google Noticias',
            'published': pub_dt,
        })
    return out
