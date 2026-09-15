"""
alerts/congresojal.py — "EPG" de JaliscoTV Parlamento a partir de la agenda
parlamentaria del Congreso de Jalisco (https://www.congresojal.gob.mx/
agenda-parlamentaria), ya que el canal transmite en función de esa agenda
(sesiones, comisiones, ruedas de prensa) y no tiene una guía de programas
tradicional -- no hay EIT real en TVHeadend para él ni cobertura en
epgshare01/open-epg.com (confirmado 2026-09-11).

Es una aproximación, no una guía exacta: la agenda lista actividades del
Congreso (algunas sí se transmiten en el canal -- sesiones, comisiones --
otras probablemente no, como ruedas de prensa o semanas temáticas), pero
no hay forma de distinguir cuáles se transmiten sin conocimiento externo,
así que se guardan TODAS como si fueran "programación" del canal. Tampoco
trae hora de fin -- se infiere como el inicio del siguiente evento del
mismo mes (o +1h por default para el último).

El sitio es Drupal con el módulo Calendar -- HTML servido del lado del
servidor (sin AJAX), con fecha en ISO 8601 en un atributo `content`. Se
navega por mes vía /agenda-parlamentaria/mes/YYYY-MM, y sí conserva
historial real (confirmado con datos reales de agosto 2026). Como los
demás scrapers de este módulo (alerts/jaliscotv.py, alerts/udgtv.py): no
es una API oficial, se rompe sin aviso si el sitio cambia; falla en
silencio sin tumbar el resto del refresh de EPG.
"""
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta

logger = logging.getLogger('congresojal')

BASE_URL     = 'https://www.congresojal.gob.mx/agenda-parlamentaria/mes/{year:04d}-{month:02d}'
TIMEOUT      = 20
CHANNEL_NAME = 'JaliscoTV Parlamento'

_EVENT_RE = re.compile(
    r'<a href="[^"]+">([^<]+)</a></span>.*?content="([^"]+)"', re.DOTALL
)


def _fetch_month(year: int, month: int) -> list[tuple[datetime, str]]:
    """[(datetime, titulo), ...] ordenado, para un mes calendario."""
    url = BASE_URL.format(year=year, month=month)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode('utf-8', errors='ignore')
    except Exception as e:
        logger.error(f"Congreso Jalisco: error consultando agenda de {year}-{month:02d}: {e}")
        return []

    out = []
    for title, iso_ts in _EVENT_RE.findall(raw):
        title = ' '.join(title.split())
        if not title:
            continue
        try:
            # ISO con offset, ej. "2026-09-01T10:00:00-05:00"
            dt = datetime.fromisoformat(iso_ts).replace(tzinfo=None)
        except Exception:
            continue
        out.append((dt, title))
    out.sort(key=lambda x: x[0])
    return out


def fetch_range(adb, months_back: int = 2, months_fwd: int = 1) -> int:
    """Guarda la agenda de [mes actual - months_back, mes actual + months_fwd]
    como programación de CHANNEL_NAME. Retorna eventos nuevos guardados."""
    from alerts.epg import ensure_schema
    ensure_schema(adb)

    today = datetime.now()
    total = 0
    for offset in range(-months_back, months_fwd + 1):
        # Suma/resta meses sin depender de librerías externas (dateutil).
        m = today.month - 1 + offset
        year, month = today.year + m // 12, m % 12 + 1
        events = _fetch_month(year, month)
        for i, (start_dt, title) in enumerate(events):
            stop_dt = events[i + 1][0] if i + 1 < len(events) else start_dt + timedelta(hours=1)
            if stop_dt <= start_dt:
                stop_dt = start_dt + timedelta(hours=1)
            try:
                cur = adb.execute("""
                    INSERT OR IGNORE INTO epg_programmes
                        (channel_name, start_ts, stop_ts, title, source)
                    VALUES (?, ?, ?, ?, 'congresojal')
                """, (CHANNEL_NAME, start_dt.strftime('%Y-%m-%d %H:%M:%S'),
                      stop_dt.strftime('%Y-%m-%d %H:%M:%S'), title))
                total += cur.rowcount
            except Exception:
                pass
    adb.commit()
    return total
