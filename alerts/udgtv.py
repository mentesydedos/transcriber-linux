"""
alerts/udgtv.py — EPG de Canal 44 (UDG TV), raspando
https://udgtv.com/canal44/programacion.

A diferencia de JaliscoTV (alerts/jaliscotv.py), esta página SÍ trae el
horario completo ya renderizado en el HTML (sin AJAX) -- pero organizado
por DÍA DE LA SEMANA recurrente (domingo=0 ... sábado=6, misma convención
que Date.getDay() de JS, confirmado en el propio JS de la página: "hoy =
new Date().getDay()"), no por fecha específica. Es decir: es una plantilla
semanal que se repite, no una guía día por día -- razonable para un canal
educativo/cultural con programación fija, pero no captura specials de un
día en particular. fetch_range() proyecta esa plantilla sobre fechas
calendario reales (pasadas y futuras) para poder guardarla en
epg_programmes junto con las demás fuentes.

data-horafinal en el HTML es idéntico a data-horainicio (no trae la hora
de fin real) -- el fin de cada bloque se infiere como el inicio del
siguiente bloque del mismo día (o medianoche para el último).

Como alerts/jaliscotv.py: scraping no oficial, se rompe sin aviso si UDG TV
cambia su sitio. Falla en silencio (lista/conteo vacío) sin tumbar el
resto del refresh de EPG.
"""
import html
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta

logger = logging.getLogger('udgtv')

URL     = 'https://udgtv.com/canal44/programacion'
TIMEOUT = 20
CHANNEL_NAME = 'Canal 44'

_BLOCK_RE = re.compile(
    r'diadeprogramacion(\d) listadodeprogramacion item\d+"'
    r' data-horainicio="([^"]+)" data-horafinal="[^"]+"><a[^>]*><img[^>]*alt="([^"]*)"'
)


def _fetch_week() -> dict[int, list[tuple[str, str]]]:
    """{dia_semana (0=domingo..6=sábado): [(HH:MM, titulo), ...] ordenado}."""
    req = urllib.request.Request(URL, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode('utf-8', errors='ignore')
    except Exception as e:
        logger.error(f"UDG TV: error consultando programación: {e}")
        return {}

    by_day: dict[int, set[tuple[str, str]]] = {}
    for day, hhmm, title in _BLOCK_RE.findall(raw):
        title = ' '.join(html.unescape(title).split())
        if not title:
            continue
        by_day.setdefault(int(day), set()).add((hhmm, title))

    return {day: sorted(entries) for day, entries in by_day.items()}


def fetch_range(adb, days_back: int = 7, days_fwd: int = 3) -> int:
    """Proyecta la plantilla semanal sobre [hoy-days_back, hoy+days_fwd] y
    guarda los programas nuevos en epg_programmes. Retorna cuántos."""
    from alerts.epg import ensure_schema
    week = _fetch_week()
    if not week:
        return 0
    ensure_schema(adb)

    total = 0
    today = datetime.now().date()
    for offset in range(-days_back, days_fwd + 1):
        date = today + timedelta(days=offset)
        js_weekday = (date.weekday() + 1) % 7  # Python Mon=0..Sun=6 -> JS Sun=0..Sat=6
        entries = week.get(js_weekday)
        if not entries:
            continue
        for i, (hhmm, title) in enumerate(entries):
            h, m = int(hhmm[:2]), int(hhmm[3:5])
            start_dt = datetime(date.year, date.month, date.day, h, m)
            if i + 1 < len(entries):
                nh, nm = int(entries[i + 1][0][:2]), int(entries[i + 1][0][3:5])
                stop_dt = datetime(date.year, date.month, date.day, nh, nm)
                if stop_dt <= start_dt:
                    stop_dt = start_dt + timedelta(minutes=30)
            else:
                stop_dt = datetime(date.year, date.month, date.day) + timedelta(days=1)
            try:
                cur = adb.execute("""
                    INSERT OR IGNORE INTO epg_programmes
                        (channel_name, start_ts, stop_ts, title, source)
                    VALUES (?, ?, ?, ?, 'udgtv')
                """, (CHANNEL_NAME, start_dt.strftime('%Y-%m-%d %H:%M:%S'),
                      stop_dt.strftime('%Y-%m-%d %H:%M:%S'), title))
                total += cur.rowcount
            except Exception:
                pass
    adb.commit()
    return total
