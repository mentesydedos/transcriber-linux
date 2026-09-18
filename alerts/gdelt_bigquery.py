"""
alerts/gdelt_bigquery.py — Búsqueda de noticias internacionales vía el
Global Knowledge Graph (GKG) de GDELT sobre Google BigQuery, en vez del
DOC 2.0 API (alerts/gdelt.py). Se agregó porque el DOC API tiene un límite
de tasa muy estricto (1 consulta/5s, y en la práctica se topó seguido
incluso respetándolo) -- BigQuery no tiene ese problema, y de paso el GKG
trae metadata mucho más rica por artículo: personas, organizaciones, temas,
tono, citas textuales (ver extra_data en cada resultado).

Requiere una cuenta de servicio de Google Cloud con rol "BigQuery User"
(ver /settings → "GDELT vía BigQuery") -- sin esas credenciales guardadas,
fetch_articles() devuelve lista vacía y alerts/watcher.py cae de vuelta al
DOC API normal (ver _poll_gdelt_for_search).

Diferencia importante con el DOC API: el GKG NO trae el título del
artículo (solo entidades extraídas + URL) -- se intenta obtener el título
real bajando la página del artículo y leyendo su <title> (ver
_fetch_real_title, timeout corto, falla en silencio); si eso falla, se cae
a un resumen CONSTRUIDO a partir de citas/personas/organizaciones (ver
_build_summary) -- mejor que nada, pero menos informativo que el título
real.

Costo: cada consulta se acota por partición de fecha (_PARTITIONTIME) --
sin esto, escanearía toda la tabla (varios TB) en cada consulta. Acotado
así, una consulta de un día ronda decenas de MB (confirmado en pruebas
reales), muy por debajo del 1 TB gratis mensual de BigQuery. Por seguridad
extra, cada consulta lleva un tope duro (`maximum_bytes_billed`) que la
CANCELA si de todos modos intentara escanear más de lo esperado -- nunca
debería generar un cargo real.
"""
import html
import json
import logging
import re
import sqlite3
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alerts.channel_types import GDELT_CHANNEL_ID, GDELT_MX_CHANNEL_ID

logger = logging.getLogger('gdelt_bigquery')

BASE_DIR  = Path(__file__).parent.parent
ALERTS_DB = BASE_DIR / 'alerts.db'

GKG_TABLE = 'gdelt-bq.gdeltv2.gkg_partitioned'
ROW_LIMIT = 2000
MAX_BYTES_BILLED = 2 * 1024 ** 3  # 2 GB -- tope duro de seguridad por consulta
# El GKG solo actualiza cada 15 min -- no tiene caso bisectar más fino que
# eso (ninguna ventana de 15 min real llega a ROW_LIMIT salvo picos
# extremos de cobertura mundial, y ahí sí se acepta el corte).
MIN_BISECT_MINUTES = 15


def _load_credentials_dict() -> dict | None:
    """Lee la llave JSON de la cuenta de servicio desde settings (guardada
    vía /settings, ver alerts/app.py). None si no está configurada."""
    try:
        conn = sqlite3.connect(str(ALERTS_DB), timeout=10)
        row = conn.execute("SELECT value FROM settings WHERE key='bigquery_credentials_json'").fetchone()
        conn.close()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except Exception as e:
        logger.error(f"GDELT BigQuery: credenciales guardadas no son JSON válido: {e}")
        return None


def _client():
    creds_dict = _load_credentials_dict()
    if not creds_dict:
        return None
    try:
        from google.cloud import bigquery
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_info(creds_dict)
        return bigquery.Client(credentials=creds, project=creds_dict['project_id'])
    except Exception as e:
        logger.error(f"GDELT BigQuery: error creando cliente: {e}")
        return None


def _strip_accents(text: str) -> str:
    """Misma normalización que alerts/watcher.py:_strip_accents -- se
    duplica aquí (3 líneas) en vez de importarla para no acoplar este
    módulo a watcher.py."""
    text = unicodedata.normalize('NFD', text)
    return ''.join(c for c in text if unicodedata.category(c) != 'Mn')


def _is_mexican_domain(domain: str) -> bool:
    """El GKG no trae el país de la fuente como tal (a diferencia del DOC
    API, que sí, ver alerts/gdelt.py:_is_mexican) -- se usa el TLD .mx como
    aproximación. Menos preciso (algunos medios mexicanos reales no usan
    .mx), pero es la única señal disponible sin una consulta aparte."""
    return (domain or '').strip().lower().endswith('.mx')


def _split_field(raw: str) -> list[str]:
    """V2Persons/V2Organizations vienen como 'Nombre,offset;Nombre,offset;...'
    -- se queda solo con los nombres, sin el offset de caracter."""
    if not raw:
        return []
    names = []
    for part in raw.split(';'):
        name = part.split(',')[0].strip()
        if name:
            names.append(name)
    # dedup preservando orden
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _parse_quotations(raw: str) -> list[str]:
    """Quotations: 'offset#length#verbo#cita#...' repetido, separado por '#'
    en grupos de 4 -- se queda solo con el texto de la cita (4to campo)."""
    if not raw:
        return []
    parts = raw.split('#')
    quotes = []
    for i in range(3, len(parts), 4):
        q = parts[i].strip()
        if q:
            quotes.append(q)
    return quotes


TITLE_FETCH_TIMEOUT = 4.0
TITLE_FETCH_WORKERS = 20  # en paralelo -- secuencial, 500 filas * 4s de timeout serían ~33 min por consulta


def _fetch_real_title(url: str) -> str | None:
    """Baja el <title> real de la página del artículo -- el GKG no lo trae
    (a diferencia del DOC API, ver docstring del módulo). Solo lee los
    primeros 64KB (el <title> siempre está cerca del inicio del <head>),
    timeout corto -- si el sitio es lento, bloquea o cambió de formato,
    simplemente no hay título real y se cae al resumen construido
    (_build_summary) sin tumbar nada."""
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=TITLE_FETCH_TIMEOUT) as r:
            raw = r.read(65536)
        text = raw.decode('utf-8', errors='ignore')
        m = re.search(r'<title[^>]*>([^<]+)</title>', text, re.IGNORECASE)
        if not m:
            return None
        title = ' '.join(html.unescape(m.group(1)).split())
        return title or None
    except Exception:
        return None


def _build_summary(persons: list[str], orgs: list[str], themes: list[str],
                    quotes: list[str], domain: str) -> str:
    """El GKG no trae título de artículo -- se construye un resumen legible
    a partir de lo más informativo disponible, en orden de preferencia."""
    if quotes:
        return quotes[0][:200]
    if persons:
        return f"Menciona a: {', '.join(persons[:4])}"
    if orgs:
        return f"Menciona a: {', '.join(orgs[:4])}"
    if themes:
        return f"Temas: {', '.join(themes[:4])}"
    return f"Artículo de {domain}"


def _parse_gkg_dt(raw) -> datetime:
    """DATE viene como YYYYMMDDHHMMSS en UTC (especificación del GKG) --
    sin convertir, quedaba guardado como si fuera hora local y todo
    aparecía ~6h adelantado en el mapa de calor y demás vistas (detectado:
    mostraba actividad a las 18h siendo las 12h reales). Se marca como UTC
    y se convierte a la hora del sistema (America/Mexico_City, fijo en
    UTC-6 desde que México eliminó el horario de verano) para cuadrar con
    TV/radio/Google Noticias en la misma tabla `matches`."""
    dt_utc = datetime.strptime(str(raw), '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
    return dt_utc.astimezone().replace(tzinfo=None)


def _query_gkg(client, kw_regexes: list[str], dt_from: datetime, dt_to: datetime) -> list:
    """Una sola consulta -- dt_from/dt_to acotan tanto la partición
    (_PARTITIONTIME, para costo) como el campo DATE (para el rango de hora
    real dentro del día, usado por la bisección).

    kw_regexes: uno o más patrones (ver fetch_articles, keyword con "+" --
    misma sintaxis que alerts/watcher.py:_match para TV/radio). Con más de
    uno, se exige que TODOS aparezcan (en cualquiera de los 4 campos, no
    necesariamente el mismo) -- un grupo OR-de-4-campos por término, y los
    términos ANDados entre sí.

    Dos filtros clave, ausentes hasta ahora:
    - Idioma: TranslationInfo trae 'srclc:<idioma origen>' cuando GDELT
      tradujo el artículo desde un idioma no inglés (NULL si ya era
      inglés) -- sin esto, una búsqueda como "mencho" traía artículos en
      ruso/árabe/lo que sea, porque el GKG cubre medios de TODO el mundo
      sin restricción de idioma (a diferencia del DOC API, que sí filtra
      por sourcelang, ver alerts/gdelt.py LANGUAGE_FILTER). Se acota a
      inglés (TranslationInfo NULL) o español (srclc:spa) -- confirmado
      con datos reales: fuentes .mx siempre traen 'srclc:spa'.
    - Coincidencia de palabra completa: LIKE %mencho% hacía match dentro de
      "Menchov", "Menchova", etc. -- confirmado con un caso real (artículo
      ruso sobre "Julia Menchov" colándose en una búsqueda de "mencho").
      REGEXP_CONTAINS con \\b (límite de palabra) a los lados del término
      evita ese falso positivo sin dejar de encontrar frases completas como
      "cartel jalisco".
    - Sin distinguir acentos: la búsqueda local de TV/radio (alerts/
      watcher.py:_match) ya es insensible a acentos por default (sin
      necesidad de activar "fonética") -- GDELT/BigQuery no lo era
      (confirmado: "cartel jalisco" sin tilde daba 1 resultado, "cártel
      jalisco" con tilde daba 0, el MISMO artículo real), así que kw_regex
      llega ya sin acentos (ver fetch_articles) y aquí se le quita el
      acento a los campos también antes de comparar (NORMALIZE a forma
      descompuesta NFD + quitar las marcas combinantes \\p{{Mn}})."""
    from google.cloud import bigquery
    def _noacc(field: str) -> str:
        return f"REGEXP_REPLACE(NORMALIZE(LOWER({field}), NFD), r'\\p{{Mn}}', '')"
    term_conds = []
    params = []
    for i, kw_regex in enumerate(kw_regexes):
        pname = f'kw{i}'
        term_conds.append(
            f"(REGEXP_CONTAINS({_noacc('V2Persons')}, @{pname}) OR REGEXP_CONTAINS({_noacc('V2Organizations')}, @{pname})"
            f" OR REGEXP_CONTAINS({_noacc('AllNames')}, @{pname}) OR REGEXP_CONTAINS({_noacc('V2Themes')}, @{pname}))"
        )
        params.append(bigquery.ScalarQueryParameter(pname, 'STRING', kw_regex))
    sql = f"""
        SELECT DATE, SourceCommonName, DocumentIdentifier,
               V2Persons, V2Organizations, V2Themes, V2Locations, V2Tone,
               Quotations, SharingImage
        FROM `{GKG_TABLE}`
        WHERE DATE(_PARTITIONTIME) BETWEEN @day_from AND @day_to
          AND DATE >= @dt_from AND DATE < @dt_to
          AND (TranslationInfo IS NULL OR REGEXP_CONTAINS(TranslationInfo, r'srclc:spa'))
          AND {' AND '.join(term_conds)}
        ORDER BY DATE DESC
        LIMIT {ROW_LIMIT}
    """
    params += [
        bigquery.ScalarQueryParameter('day_from', 'DATE', dt_from.date().isoformat()),
        bigquery.ScalarQueryParameter('day_to', 'DATE', dt_to.date().isoformat()),
        bigquery.ScalarQueryParameter('dt_from', 'INT64', int(dt_from.strftime('%Y%m%d%H%M%S'))),
        bigquery.ScalarQueryParameter('dt_to', 'INT64', int(dt_to.strftime('%Y%m%d%H%M%S'))),
    ]
    job_config = bigquery.QueryJobConfig(query_parameters=params, maximum_bytes_billed=MAX_BYTES_BILLED)
    try:
        return list(client.query(sql, job_config=job_config).result())
    except Exception as e:
        logger.error(f"GDELT BigQuery: error consultando {dt_from}–{dt_to}: {e}")
        return []


# Profundidad hasta la que se paraliza la bisección (2 hilos por nivel,
# cada uno con su propio ThreadPoolExecutor de vida corta -- no uno
# compartido, para no arriesgar un deadlock si un hilo recursivo tuviera
# que esperar un turno libre en el mismo pool que lo está ejecutando).
# Más allá de esto ya no vale la pena: las ventanas son chicas y el costo
# de más hilos no compensa. BigQuery Client es seguro para consultas
# concurrentes (cada .query() dispara su propio job).
BISECT_PARALLEL_MAX_DEPTH = 3


def _query_gkg_bisect(client, kw_regexes: list[str], dt_from: datetime, dt_to: datetime, _depth: int = 0) -> list:
    """Si la consulta topa ROW_LIMIT, ORDER BY DATE DESC descarta en
    silencio todo lo más viejo de la ventana -- para una palabra frecuente
    (ej. "mexico": 1495 menciones/día reales) eso dejaba huecos de horas
    completas en el mapa de calor, no porque no hubiera cobertura sino
    porque se cortaba. Se bisecta el rango de tiempo a la mitad y se repite
    hasta que quepa completo o hasta MIN_BISECT_MINUTES (piso real de
    actualización del GKG). Las primeras BISECT_PARALLEL_MAX_DEPTH
    bisecciones corren en paralelo (BigQuery no tiene límite de tasa, a
    diferencia del DOC API de alerts/gdelt.py)."""
    rows = _query_gkg(client, kw_regexes, dt_from, dt_to)
    span_min = (dt_to - dt_from).total_seconds() / 60
    if len(rows) < ROW_LIMIT or span_min <= MIN_BISECT_MINUTES or _depth > 20:
        return rows
    mid = dt_from + (dt_to - dt_from) / 2
    if _depth < BISECT_PARALLEL_MAX_DEPTH:
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_left  = pool.submit(_query_gkg_bisect, client, kw_regexes, dt_from, mid, _depth + 1)
            fut_right = pool.submit(_query_gkg_bisect, client, kw_regexes, mid, dt_to, _depth + 1)
            left, right = fut_left.result(), fut_right.result()
    else:
        left  = _query_gkg_bisect(client, kw_regexes, dt_from, mid, _depth + 1)
        right = _query_gkg_bisect(client, kw_regexes, mid, dt_to, _depth + 1)
    return left + right


def fetch_articles(query: str, date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    """Devuelve [{title, link, source, source_domain, published, channel_id,
    extra_data}, ...] -- mismo shape base que alerts.gdelt/googlenews (para
    reusar _poll_articles_for_search en alerts/watcher.py), más
    `extra_data` (dict serializable a JSON con personas/organizaciones/
    temas/tono/citas, ver alerts/app.py columna matches.extra_data).

    date_from/date_to en 'YYYY-MM-DD' (inclusive); sin fechas usa los
    últimos 2 días (poll en vivo). El campo DATE del GKG es UTC, así que
    los límites se calculan también en UTC (ver _parse_gkg_dt)."""
    client = _client()
    if not client:
        return []

    if date_from and date_to:
        dt_from = datetime.strptime(date_from, '%Y-%m-%d')
        dt_to   = datetime.strptime(date_to, '%Y-%m-%d') + timedelta(days=1)
    else:
        dt_to   = datetime.now(timezone.utc).replace(tzinfo=None)
        dt_from = dt_to - timedelta(days=2)

    # \b a los lados = palabra/frase completa, no substring (evita que
    # "mencho" matchee dentro de "Menchov", ver docstring de _query_gkg) --
    # todo en minúsculas y sin acentos porque los campos del GKG también se
    # comparan ya sin acentos (REGEXP_CONTAINS distingue mayúsculas Y
    # acentos, a diferencia de la búsqueda local que ya es accent-
    # insensitive por default, ver alerts/watcher.py:_strip_accents).
    #
    # query puede traer "+" -- varios términos que deben aparecer TODOS
    # (en cualquier campo, no necesariamente el mismo ni juntos), misma
    # sintaxis que alerts/watcher.py:_match para TV/radio. Antes cada "+"
    # se escapaba como parte de UN solo patrón literal (nunca aparece así
    # en un artículo real, esa keyword no encontraba nada -- confirmado
    # con "independencia+mexico", 2026-09-17).
    terms = [t.strip() for t in query.split('+') if t.strip()] or [query]
    kw_regexes = [r'\b' + re.escape(_strip_accents(t.lower())) + r'\b' for t in terms]
    rows = _query_gkg_bisect(client, kw_regexes, dt_from, dt_to)

    parsed = []
    for row in rows:
        link   = row['DocumentIdentifier'] or ''
        domain = row['SourceCommonName'] or ''
        if not link:
            continue
        try:
            pub_dt = _parse_gkg_dt(row['DATE'])
        except Exception:
            continue

        persons = _split_field(row['V2Persons'])
        orgs    = _split_field(row['V2Organizations'])
        themes  = _split_field(row['V2Themes'])
        quotes  = _parse_quotations(row['Quotations'])
        parsed.append((link, domain, pub_dt, persons, orgs, themes, quotes, row))

    # Títulos reales en paralelo -- uno por uno tomaría hasta
    # ROW_LIMIT * TITLE_FETCH_TIMEOUT (~33 min para 500 filas), inviable
    # para un poll que corre cada 2 horas (ver GDELT_POLL_MINUTES en
    # alerts/watcher.py).
    links = [p[0] for p in parsed]
    with ThreadPoolExecutor(max_workers=TITLE_FETCH_WORKERS) as pool:
        real_titles = list(pool.map(_fetch_real_title, links))

    out = []
    for (link, domain, pub_dt, persons, orgs, themes, quotes, row), real_title in zip(parsed, real_titles):
        # Máximo contexto posible: título real del artículo + una cita
        # textual destacada si hay una y no está ya contenida en el título.
        # Sin título real, se cae al resumen construido de personas/
        # organizaciones/temas.
        if real_title:
            title = real_title
            if quotes and quotes[0] not in real_title:
                title += f' — "{quotes[0][:150]}"'
        else:
            title = _build_summary(persons, orgs, themes, quotes, domain)

        out.append({
            'title':         title,
            'link':          link,
            'source':        domain or 'GDELT',
            'source_domain': domain,
            'published':     pub_dt,
            'channel_id':    GDELT_MX_CHANNEL_ID if _is_mexican_domain(domain) else GDELT_CHANNEL_ID,
            'extra_data':    {
                'personas':       persons,
                'organizaciones': orgs,
                'temas':          themes,
                'citas':          quotes,
                'tono':           row['V2Tone'],
                'ubicaciones':    row['V2Locations'],
                'imagen':         row['SharingImage'],
            },
        })
    return out


# fetch_articles ya bisecta internamente por tiempo cuando hace falta (ver
# _query_gkg_bisect) sin importar qué tan amplio sea el rango date_from/
# date_to -- no hace falta una función de rango aparte, a diferencia de
# alerts/googlenews.py y alerts/gdelt.py.
fetch_articles_range = fetch_articles
