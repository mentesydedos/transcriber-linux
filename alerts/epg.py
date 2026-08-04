"""
alerts/epg.py — Guía de programación (EPG) desde múltiples fuentes.

Fuente 1 — epgshare01.online  (archivos MX1 y MX2)
  Cubre canales nacionales y algunos regionales mexicanos.
  El canal correcto se detecta automáticamente comparando nombres.
  Se actualiza cada 12 horas.

Fuente 2 — TVHeadend EIT
  TVHeadend captura tablas EIT de la señal de cable (Megacable) y las
  expone vía /api/epg/events/grid. Cubre canales regionales Guadalajara
  como Imagen y ADN 40.  Se actualiza cada 6 horas.
  Requiere que el usuario TVHeadend tenga permiso de API (Web interface).

Los datos se acumulan INDEFINIDAMENTE (nunca se borran).
"""

import gzip
import json
import re
import sqlite3
import logging
import base64
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

logger = logging.getLogger('epg')

BASE_DIR  = Path(__file__).parent.parent
ALERTS_DB = BASE_DIR / 'alerts.db'

# ── Fuente 1: epgshare01 ──────────────────────────────────────────────────────
# Ambos archivos se prueban; el segundo suele incluir canales regionales / cable.
EPG_SOURCES = [
    # MX1 cubre: Azteca Uno, Las Estrellas, Canal 5, Canal 4, Azteca 7, ADN 40
    # N+ y Imagen se obtienen de TVHeadend EIT (si la señal EIT está disponible)
    # Imagen: sin fuente EPG disponible (Megacable no transmite EIT para ese canal)
    'https://epgshare01.online/epgshare01/epg_ripper_MX1.xml.gz',
]
REFRESH_HOURS = 12

# Canales que queremos obtener de epgshare01.
# Valor: lista de fragmentos (minúsculas) a buscar en el id/nombre del canal.
# El primero que coincida gana.  Añade más fragmentos si el canal cambia de ID.
EPGSHARE_WANTED = {
    'Las Estrellas': ['estrellas', 'xew'],
    'Canal 5':       ['canal.5.de', 'xhgc', 'canal cinco', 'canal5', 'canal 5'],
    'Azteca Uno':    ['azteca.uno', 'aztecauno'],
    'Canal 4':       ['foro.tv', 'forotv', 'canal.4.de'],
    'Azteca 7':      ['azteca.7', 'azteca7', 'xhlat'],
    'N+':            ['nmas', 'n.mas', 'nmás', 'n+'],
    'Imagen':        ['imagen.telev', 'imagen tv', 'imagentv', 'imagen.tv',
                      'xhimt', 'canal imagen', 'imagentv.mx'],
    'ADN 40':        ['adn.40', 'adn40', 'adn 40'],
    # Canales agregados en la ampliacion a 26 (2026-08-04) que epgshare01 MX1
    # ya trae pero no estaban mapeados.
    'Canal 11':      ['11.de.méxico', 'canal 11 de méxico'],
    'Canal 14':      ['14.de.méxico', 'canal 14 de méxico'],
    'Canal 22':      ['22.de.méxico', 'canal 22 de méxico'],
    'TV UNAM':       ['tvunam', 'tv unam'],
    'Excélsior TV':  ['excelsior'],
}

# ── Fuente 2: TVHeadend EIT ───────────────────────────────────────────────────
TVH_BASE          = 'http://148.201.38.136:9981'
TVH_USER          = 'poncho'
TVH_PASS          = '1234'
TVH_REFRESH_HOURS = 6

# Fragmento del nombre en TVHeadend (minúsculas) → nombre interno
TVH_CHANNEL_MAP = {
    'imagen':      'Imagen',
    'adn':         'ADN 40',
    'n+':          'N+',
    'nmas':        'N+',
    'n mas':       'N+',
    'canal cinco': 'Canal 5',
    'canal5':      'Canal 5',
    # epgshare01 no cubre este canal; TVHeadend si trae EIT real (confirmado
    # 2026-08-04: titulos de programa reales, no placeholder).
    'a más +':     'A más +',
}


# ── Conexión ──────────────────────────────────────────────────────────────────
def _adb():
    c = sqlite3.connect(str(ALERTS_DB), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


# ── Esquema ───────────────────────────────────────────────────────────────────
def ensure_schema(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS epg_programmes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_name TEXT NOT NULL,
            start_ts     TEXT NOT NULL,
            stop_ts      TEXT NOT NULL,
            title        TEXT,
            source       TEXT DEFAULT 'epgshare01'
        )
    """)
    db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_epg_unique
        ON epg_programmes(channel_name, start_ts)
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_epg_lookup
        ON epg_programmes(channel_name, start_ts, stop_ts)
    """)
    db.commit()


# ── Parseo de timestamps XMLTV ────────────────────────────────────────────────
def _parse_ts(ts_str: str) -> str:
    """Convierte '20260418103001 -0600' → '2026-04-18 10:30:01' (hora local naive)."""
    ts_str = (ts_str or '').strip()
    if not ts_str:
        return ''
    try:
        dt = datetime.strptime(ts_str, '%Y%m%d%H%M%S %z')
        return dt.astimezone().replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S')
    except ValueError:
        pass
    try:
        dt = datetime.strptime(ts_str[:14], '%Y%m%d%H%M%S')
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except ValueError:
        return ''


# ── Auto-detección de IDs en epgshare01 ──────────────────────────────────────
def _build_reverse_map(root) -> dict[str, str]:
    """
    Recorre los elementos <channel> del XML y construye un mapa
    { epg_channel_id → nuestro_nombre } usando EPGSHARE_WANTED.
    """
    # Recopilar todos los canales del archivo: id → (display_names...)
    epg_channels: dict[str, list[str]] = {}
    for ch in root.findall('channel'):
        cid = ch.get('id', '')
        names = [dn.text or '' for dn in ch.findall('display-name') if dn.text]
        if cid and names:
            epg_channels[cid] = names

    reverse: dict[str, str] = {}
    for our_name, fragments in EPGSHARE_WANTED.items():
        found = False
        for cid, names in epg_channels.items():
            # Buscar en el id y en cada display-name
            haystack = (cid + ' ' + ' '.join(names)).lower()
            if any(frag in haystack for frag in fragments):
                reverse[cid] = our_name
                logger.info(
                    f"EPG auto: '{our_name}' → '{names[0]}' ({cid})"
                )
                found = True
                break
        if not found:
            logger.debug(f"EPG auto: '{our_name}' no encontrado en este archivo")

    return reverse


# ── Fuente 1: fetch epgshare01 ────────────────────────────────────────────────
def _fetch_xmltv_url(url: str) -> tuple[int, set[str]]:
    """
    Descarga un archivo XMLTV (.xml.gz), detecta automáticamente los canales
    mapeados y guarda los programas nuevos.
    Retorna (n_nuevos, set de nombres cubiertos).
    """
    logger.info(f"EPG: descargando {url} …")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = gzip.decompress(r.read())
    except Exception as e:
        logger.error(f"EPG fetch error ({url}): {e}")
        return 0, set()

    try:
        root = ET.fromstring(raw)
    except Exception as e:
        logger.error(f"EPG parse error ({url}): {e}")
        return 0, set()

    reverse = _build_reverse_map(root)
    if not reverse:
        logger.warning(f"EPG: ningún canal mapeado en {url}")
        return 0, set()

    adb = _adb()
    ensure_schema(adb)
    count = 0

    for prog in root.findall('programme'):
        cid     = prog.get('channel', '')
        ch_name = reverse.get(cid)
        if not ch_name:
            continue

        start_ts = _parse_ts(prog.get('start', ''))
        stop_ts  = _parse_ts(prog.get('stop',  ''))
        if not start_ts or not stop_ts:
            continue

        title = ' '.join((prog.findtext('title') or '').split())
        title = re.sub(r'^\[[A-Z]\]\s*', '', title)

        try:
            cur = adb.execute("""
                INSERT OR IGNORE INTO epg_programmes
                    (channel_name, start_ts, stop_ts, title, source)
                VALUES (?, ?, ?, ?, 'epgshare01')
            """, (ch_name, start_ts, stop_ts, title))
            count += cur.rowcount
        except Exception:
            pass

    adb.commit()
    adb.close()

    covered = set(reverse.values())
    logger.info(f"EPG {url.split('/')[-1]}: {count} programas nuevos — canales: {covered}")
    return count, covered


def fetch_current() -> int:
    """
    Descarga todos los archivos EPG configurados en EPG_SOURCES.
    Retorna el total de programas nuevos guardados.
    """
    total   = 0
    covered: set[str] = set()

    for url in EPG_SOURCES:
        n, ch_set = _fetch_xmltv_url(url)
        total   += n
        covered |= ch_set

    # Canales que siguen sin cobertura tras todas las fuentes
    wanted_names = set(EPGSHARE_WANTED.keys())
    missing = wanted_names - covered
    if missing:
        logger.info(
            f"EPG epgshare01: sin cobertura para {missing} "
            "(se intentará vía TVHeadend EIT)"
        )

    return total


# ── Fuente 2: fetch TVHeadend EIT ─────────────────────────────────────────────
def _tvh_opener():
    """Crea un opener urllib con soporte Digest + Basic (TVHeadend usa Digest)."""
    pwd = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    pwd.add_password(None, TVH_BASE, TVH_USER, TVH_PASS)
    return urllib.request.build_opener(
        urllib.request.HTTPDigestAuthHandler(pwd),
        urllib.request.HTTPBasicAuthHandler(pwd),
    )

def _tvh_request(path: str, timeout: int = 15):
    url    = f'{TVH_BASE}{path}'
    opener = _tvh_opener()
    req    = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def fetch_tvheadend() -> int:
    """
    Obtiene EPG desde TVHeadend (tablas EIT de la señal de cable).
    Retorna n programas nuevos, o -1 si el API no está accesible.
    """
    try:
        data = _tvh_request('/api/channel/grid?limit=500&offset=0')
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            logger.warning(
                f"TVH EPG: HTTP {e.code} — credenciales incorrectas o sin permiso 'Web interface'. "
                f"Revisa usuario '{TVH_USER}' en TVHeadend → Configuration → Users."
            )
        else:
            logger.error(f"TVH EPG: HTTP {e.code} al obtener canales")
        return -1
    except Exception as e:
        logger.error(f"TVH EPG: error al obtener canales: {e}")
        return -1

    # Mapear UUIDs a nombres internos
    channel_uuids: dict[str, str] = {}
    for ch in data.get('entries', []):
        name_lower = (ch.get('name') or '').lower()
        for fragment, our_name in TVH_CHANNEL_MAP.items():
            if fragment in name_lower:
                channel_uuids[ch['uuid']] = our_name
                logger.info(f"TVH EPG: '{ch['name']}' → '{our_name}'")
                break

    if not channel_uuids:
        logger.warning(
            f"TVH EPG: ningún canal coincidió con {list(TVH_CHANNEL_MAP.keys())}."
        )
        return 0

    adb = _adb()
    ensure_schema(adb)
    total = 0

    for uuid, our_name in channel_uuids.items():
        offset = 0
        limit  = 1000
        while True:
            try:
                epg = _tvh_request(
                    f'/api/epg/events/grid?channel={uuid}'
                    f'&limit={limit}&start={offset}'
                )
            except Exception as e:
                logger.error(f"TVH EPG: error eventos '{our_name}': {e}")
                break

            entries = epg.get('entries', [])
            if not entries:
                break

            for ev in entries:
                unix_start = ev.get('start', 0)
                unix_stop  = ev.get('stop',  0)
                if not unix_start or not unix_stop:
                    continue
                start_ts = datetime.fromtimestamp(unix_start).strftime('%Y-%m-%d %H:%M:%S')
                stop_ts  = datetime.fromtimestamp(unix_stop ).strftime('%Y-%m-%d %H:%M:%S')
                title    = ' '.join((ev.get('title') or '').split())

                try:
                    cur = adb.execute("""
                        INSERT OR IGNORE INTO epg_programmes
                            (channel_name, start_ts, stop_ts, title, source)
                        VALUES (?, ?, ?, ?, 'tvheadend')
                    """, (our_name, start_ts, stop_ts, title))
                    total += cur.rowcount
                except Exception:
                    pass

            offset += len(entries)
            if len(entries) < limit:
                break

    adb.commit()
    adb.close()
    logger.info(f"TVH EPG total: {total} programas nuevos.")
    return total


# ── Refresco automático ───────────────────────────────────────────────────────
def _needs_refresh(db, key: str, hours: float) -> bool:
    row = db.execute(
        "SELECT value FROM settings WHERE key=?", (key,)
    ).fetchone()
    if not row:
        return True
    try:
        last    = datetime.strptime(row['value'], '%Y-%m-%d %H:%M:%S')
        elapsed = (datetime.now() - last).total_seconds() / 3600
        return elapsed >= hours
    except Exception:
        return True


def needs_refresh(db) -> bool:
    return _needs_refresh(db, 'epg_last_fetch', REFRESH_HOURS)


def refresh_if_needed(db):
    ensure_schema(db)
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Fuente 1: epgshare01 MX1 + MX2 (cada 12 h)
    if _needs_refresh(db, 'epg_last_fetch', REFRESH_HOURS):
        n = fetch_current()
        if n >= 0:
            db.execute(
                "INSERT OR REPLACE INTO settings (key,value) VALUES ('epg_last_fetch',?)",
                (now_str,)
            )
            db.commit()

    # Fuente 2: TVHeadend EIT (cada 6 h; backoff 30 min si falla)
    if _needs_refresh(db, 'epg_tvh_last_fetch', TVH_REFRESH_HOURS):
        n = fetch_tvheadend()
        if n >= 0:
            # Éxito: marcar con hora actual → siguiente intento en 6 h
            db.execute(
                "INSERT OR REPLACE INTO settings (key,value) VALUES ('epg_tvh_last_fetch',?)",
                (now_str,)
            )
        else:
            # Fallo: marcar con hora actual - 5.5 h → reintento en 30 min
            from datetime import timedelta
            backoff = (datetime.now() - timedelta(hours=TVH_REFRESH_HOURS - 0.5)).strftime('%Y-%m-%d %H:%M:%S')
            db.execute(
                "INSERT OR REPLACE INTO settings (key,value) VALUES ('epg_tvh_last_fetch',?)",
                (backoff,)
            )
        db.commit()


# ── Consultas ─────────────────────────────────────────────────────────────────
def get_programme_at(db, channel_name: str, timestamp: str) -> str:
    if not channel_name or not timestamp:
        return ''
    row = db.execute("""
        SELECT title FROM epg_programmes
        WHERE channel_name = ? AND start_ts <= ? AND stop_ts > ?
        ORDER BY start_ts DESC LIMIT 1
    """, (channel_name, timestamp, timestamp)).fetchone()
    return (row['title'] or '') if row else ''


def get_coverage_stats(db) -> list:
    """Estadísticas de cobertura EPG por canal; útil para el panel de ajustes."""
    rows = db.execute("""
        SELECT channel_name,
               COUNT(*)      as total,
               MIN(start_ts) as desde,
               MAX(stop_ts)  as hasta,
               GROUP_CONCAT(DISTINCT source) as fuentes
        FROM epg_programmes
        GROUP BY channel_name
        ORDER BY channel_name
    """).fetchall()
    return [dict(r) for r in rows]
