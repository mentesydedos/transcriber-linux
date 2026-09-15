"""
alerts/jaliscotv.py — EPG de los dos subcanales del multiplex digital 17 de
Jalisco TV (17.1 = "JaliscoTV", 17.2 = "JALISCOTV HD"), raspando el widget
de horarios de https://jaliscotv.com/programacion-tv/.

Ni epgshare01 ni TVHeadend EIT cubren estos canales (confirmado
2026-09-11): epgshare01 no los tiene en su catálogo, y Megacable no
transmite EIT real para ellos (el evento EIT que sí llega solo repite el
nombre del canal como "programa"). La propia web de Jalisco TV sí tiene una
guía real para ambos, servida por el plugin de WordPress "tv-schedule" vía
AJAX a admin-ajax.php -- no es una API pública ni documentada, así que es
la fuente MÁS frágil de las que tiene esta app: si Jalisco TV cambia de
plugin o de sitio, esto deja de funcionar sin aviso (a diferencia de
epgshare01/TVHeadend, con cierta estabilidad). fetch_schedule() ya maneja
cualquier error de red/parseo devolviendo lista vacía, sin tumbar el resto
del refresh de EPG.

Nota sobre 17.2: en un primer vistazo (pocos títulos) parecía contenido
judicial/legislativo ("Noticiero Científico y Cultural" -- que también
aparece en el feed nacional de open-epg.com bajo "Justicia TV.mx", ver
alerts/epg.py OPENEPG_WANTED), pero revisando una semana completa de
títulos se ve que en realidad comparte mucha programación literal con el
17.1 ("Jalisco Noticias", "Jalisco Aprende", "Jalisco Contigo", "Jalisco
Tierra de Campeones", "Pedalea") -- es el canal hermano JALISCOTV HD, no
Judicial TV. Esa coincidencia de nombre fue casualidad (formato de
noticiero genérico que varios canales públicos usan), no el mismo canal.
"""
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

logger = logging.getLogger('jaliscotv')

AJAX_URL = 'https://jaliscotv.com/wp-admin/admin-ajax.php'
TIMEOUT  = 20

# slug de canal en el sitio -> nuestro nombre de canal. Confirmado vía
# https://jaliscotv.com/extvs_channel-sitemap.xml + títulos de <title> de
# cada /tvs-cat/<slug>/ ("JALTV 17.1" / "JALTV 17.2").
CHANNELS = {
    'jaltv-17-1': 'JaliscoTV',
    'jaltv-17-2': 'JALISCOTV HD',
}

# Resto de parámetros del shortcode que la propia página usa (confirmado
# 2026-09-11 vía su REST API, /wp-json/wp/v2/pages/); estables mientras no
# cambien de plugin de horarios. "channel" se sobreescribe por canal.
_PARAM_SHORTCODE_BASE = {
    'style': '3', 'fullcontent_in': 'tooltip', 'show_image': 'show',
    'channel_display': 'all', 'range_timeline': '30m',
    'scroll_time': '06:00', 'slidesshow': '', 'slidesscroll': '', 'start_on': '',
    'min_time': '', 'max_time': '', 'before_today': '', 'after_today': '',
    'list_dates': '', 'order': 'DESC', 'orderby': 'date', 'meta_key': '',
    'meta_value': '', 'order_channel': '', 'class': '', 'ID': 'ex-8810',
}

_BLOCK_RE = re.compile(
    r'<h4>(\d{1,2}:\d{2}\s*[ap]m)\s*-\s*(\d{1,2}:\d{2}\s*[ap]m)</h4>\s*'
    r'<h3><a[^>]*>([^<]+)</a>', re.IGNORECASE
)


def _parse_hour(date_base: datetime, hhmm_ampm: str) -> datetime:
    """'12:00 am' / '6:30 pm' -> datetime del mismo día que date_base."""
    t = datetime.strptime(hhmm_ampm.strip().upper().replace(' ', ''), '%I:%M%p')
    return date_base.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)


def fetch_schedule(slug: str, date_str: str) -> list[dict]:
    """Devuelve [{title, start_ts, stop_ts}] para el canal `slug` (una
    llave de CHANNELS) en la fecha date_str ('YYYY-MM-DD'). Los horarios
    que trae el sitio ya están en hora de México (confirmado comparando
    contra la hora real de transmisión), la fecha solo se usa como llave
    de selección de día -- por eso se pide como medianoche UTC de ese día,
    tal cual lo hace el propio sitio."""
    day = datetime.strptime(date_str, '%Y-%m-%d')
    epoch = int(day.replace(tzinfo=timezone.utc).timestamp())
    param = dict(_PARAM_SHORTCODE_BASE, channel=slug)

    body = urllib.parse.urlencode({
        'action': 'extvs_get_schedule_advance',
        'param_shortcode': json.dumps(param),
        'date': str(epoch),
    }).encode()
    req = urllib.request.Request(AJAX_URL, data=body, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
    except Exception as e:
        logger.error(f"JaliscoTV: error consultando horario de {slug} {date_str}: {e}")
        return []

    try:
        html = json.loads(raw).get('html', '')
    except Exception as e:
        logger.error(f"JaliscoTV: error parseando JSON de {slug} {date_str}: {e}")
        return []

    out = []
    for start_raw, stop_raw, title in _BLOCK_RE.findall(html):
        try:
            start_dt = _parse_hour(day, start_raw)
            stop_dt  = _parse_hour(day, stop_raw)
            if stop_dt <= start_dt:
                stop_dt += timedelta(days=1)  # bloque que cruza medianoche
        except Exception:
            continue
        title = ' '.join(title.split())
        if not title:
            continue
        out.append({
            'title':    title,
            'start_ts': start_dt.strftime('%Y-%m-%d %H:%M:%S'),
            'stop_ts':  stop_dt.strftime('%Y-%m-%d %H:%M:%S'),
        })
    return out


def fetch_range(adb, days_back: int = 3, days_fwd: int = 1) -> int:
    """Guarda el horario de ambos subcanales (CHANNELS) para
    [hoy-days_back, hoy+days_fwd]. Retorna programas nuevos guardados.
    `adb` es la conexión a alerts.db (ensure_schema se corre aquí)."""
    from alerts.epg import ensure_schema
    ensure_schema(adb)
    total = 0
    today = datetime.now().date()
    for slug, channel_name in CHANNELS.items():
        for offset in range(-days_back, days_fwd + 1):
            date_str = (today + timedelta(days=offset)).isoformat()
            for prog in fetch_schedule(slug, date_str):
                try:
                    cur = adb.execute("""
                        INSERT OR IGNORE INTO epg_programmes
                            (channel_name, start_ts, stop_ts, title, source)
                        VALUES (?, ?, ?, ?, 'jaliscotv')
                    """, (channel_name, prog['start_ts'], prog['stop_ts'], prog['title']))
                    total += cur.rowcount
                except Exception:
                    pass
            time.sleep(1)  # cortesía -- no es una API pública, no hay que insistirle
    adb.commit()
    return total
