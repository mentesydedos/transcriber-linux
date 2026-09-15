"""
alerts/world_pulse.py — "Pulso del mundo": tendencias del día (personas,
organizaciones, lugares, tono) agregadas directamente del GKG (BigQuery)
SIN filtrar por ninguna palabra clave de búsqueda -- a diferencia de
alerts/gdelt_bigquery.py, que siempre acota por keyword de una búsqueda.

Costo: BigQuery cobra por columnas leídas × filas de las particiones
escaneadas, NO por cuántas filas sobreviven el WHERE -- una consulta "sin
keyword" para un día cuesta lo mismo que una consulta acotada por keyword
para ese mismo día y esas mismas columnas (confirmado en alerts/
gdelt_bigquery.py: "decenas de MB" por día). La agregación (top personas/
organizaciones/lugares, promedio de tono) se hace del lado de BigQuery con
UNNEST + QUALIFY/GROUP BY -- así nunca se trae a Python la lista completa
de menciones de un día entero (que puede ser de decenas de miles de filas
sin acotar por keyword), solo el resultado ya agregado.

Cacheado en alerts.db (world_pulse_cache) para no volver a pagar la
consulta en cada vista de la página -- se refresca cada CACHE_TTL_MIN para
el día de hoy; un día ya cerrado se cachea para siempre (no cambia).
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alerts.gdelt_bigquery import _client, GKG_TABLE, MAX_BYTES_BILLED

BASE_DIR  = Path(__file__).parent.parent
ALERTS_DB = BASE_DIR / 'alerts.db'

TOP_ENTITIES_N  = 15
TOP_STORIES_N   = 10
CANDIDATES_PER_STORY = 3  # candidatos de respaldo si el mejor bloquea la descarga del título (ver get_pulse)
TOP_LOCATIONS_N = 40
CACHE_TTL_MIN   = 120  # debe ser mayor a REFRESH_MIN
# Cada cuánto refresca el caché el hilo en segundo plano (ver
# alerts/watcher.py:_world_pulse_loop) -- se define aquí (no en watcher.py)
# para que quede junto a CACHE_TTL_MIN, con quien tiene que mantenerse
# sincronizado (REFRESH_MIN < CACHE_TTL_MIN, si no, alguien paga el costo
# de la consulta en vivo -- ver historial de este archivo).
REFRESH_MIN = 115

# Mismo filtro de idioma que alerts/gdelt_bigquery.py -- ver ahí por qué
# (TranslationInfo NULL = original en inglés, 'srclc:spa' = original en
# español; cualquier otro idioma queda fuera).
LANG_COND = "(TranslationInfo IS NULL OR REGEXP_CONTAINS(TranslationInfo, r'srclc:spa'))"

# Nombres que el GKG extrae con mucha frecuencia pero que NO son "la
# noticia" -- agencias de noticias (aparecen como organización en CASI
# cualquier artículo que republican, por firma, no porque sean el tema),
# redes sociales mencionadas de forma incidental, y países/gentilicios tan
# genéricos que no dicen nada por sí solos. Confirmado con datos reales:
# sin este filtro, "Reuters"/"Instagram"/"United States" ocupaban lugares
# del top 10 con artículos que no tratan de ellos en absoluto.
ENTITY_STOPLIST = {
    'reuters', 'associated press', 'agence france-presse', 'afp', 'getty images',
    'instagram', 'facebook', 'twitter', 'tiktok', 'youtube',
    'united states', 'young', 'american', 'americans', 'canadian', 'chinese', 'british',
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS world_pulse_cache (
        day         TEXT NOT NULL,
        scope       TEXT NOT NULL,
        data        TEXT NOT NULL,
        computed_at TEXT NOT NULL,
        PRIMARY KEY (day, scope)
    )""")
    conn.commit()


def _cache_get(day: str, scope: str) -> dict | None:
    conn = sqlite3.connect(str(ALERTS_DB), timeout=10)
    row = conn.execute("SELECT data, computed_at FROM world_pulse_cache WHERE day=? AND scope=?",
                        (day, scope)).fetchone()
    conn.close()
    if not row:
        return None
    data_raw, computed_at = row
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if day == today:
        age_min = (datetime.now() - datetime.strptime(computed_at, '%Y-%m-%d %H:%M:%S')).total_seconds() / 60
        if age_min > CACHE_TTL_MIN:
            return None
    try:
        return json.loads(data_raw)
    except Exception:
        return None


def _cache_set(day: str, scope: str, data: dict) -> None:
    conn = sqlite3.connect(str(ALERTS_DB), timeout=10)
    conn.execute("""INSERT INTO world_pulse_cache (day, scope, data, computed_at) VALUES (?,?,?,?)
                     ON CONFLICT(day, scope) DO UPDATE SET data=excluded.data, computed_at=excluded.computed_at""",
                 (day, scope, json.dumps(data, ensure_ascii=False),
                  datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    conn.close()


def _run(client, sql: str, params: list):
    from google.cloud import bigquery
    job_config = bigquery.QueryJobConfig(query_parameters=params, maximum_bytes_billed=MAX_BYTES_BILLED)
    return list(client.query(sql, job_config=job_config).result())


def _query_top_entities(client, day: str, mx_only: bool) -> dict:
    """Top personas y organizaciones del día -- QUALIFY + ROW_NUMBER hace el
    "top N por grupo" dentro de BigQuery mismo, así solo bajan TOP_ENTITIES_N
    filas por tipo, nunca la lista completa de nombres distintos del día
    (que fácilmente son miles sin acotar por keyword)."""
    from google.cloud import bigquery
    domain_filter = "AND SourceCommonName LIKE '%.mx'" if mx_only else ""
    sql = f"""
        WITH base AS (
          SELECT DocumentIdentifier, V2Persons, V2Organizations
          FROM `{GKG_TABLE}`
          WHERE DATE(_PARTITIONTIME) = @day AND {LANG_COND} {domain_filter}
        ),
        persons AS (
          SELECT DISTINCT DocumentIdentifier, TRIM(SPLIT(p, ',')[OFFSET(0)]) AS name
          FROM base, UNNEST(SPLIT(V2Persons, ';')) AS p WHERE p != ''
        ),
        orgs AS (
          SELECT DISTINCT DocumentIdentifier, TRIM(SPLIT(o, ',')[OFFSET(0)]) AS name
          FROM base, UNNEST(SPLIT(V2Organizations, ';')) AS o WHERE o != ''
        ),
        ranked AS (
          SELECT 'person' AS kind, name, COUNT(*) AS mentions FROM persons
          WHERE name != '' AND LOWER(name) NOT IN UNNEST(@stoplist) GROUP BY name
          UNION ALL
          SELECT 'org' AS kind, name, COUNT(*) AS mentions FROM orgs
          WHERE name != '' AND LOWER(name) NOT IN UNNEST(@stoplist) GROUP BY name
        )
        SELECT kind, name, mentions FROM ranked
        QUALIFY ROW_NUMBER() OVER (PARTITION BY kind ORDER BY mentions DESC) <= @top_n
        ORDER BY kind, mentions DESC
    """
    params = [
        bigquery.ScalarQueryParameter('day', 'DATE', day),
        bigquery.ScalarQueryParameter('top_n', 'INT64', TOP_ENTITIES_N),
        bigquery.ArrayQueryParameter('stoplist', 'STRING', sorted(ENTITY_STOPLIST)),
    ]
    rows = _run(client, sql, params)
    persons = [(r['name'], r['mentions']) for r in rows if r['kind'] == 'person']
    orgs    = [(r['name'], r['mentions']) for r in rows if r['kind'] == 'org']
    return {
        'persons': {'labels': [n for n, _ in persons][::-1], 'values': [c for _, c in persons][::-1]},
        'orgs':    {'labels': [n for n, _ in orgs][::-1],    'values': [c for _, c in orgs][::-1]},
    }


def _query_top_stories(client, day: str, mx_only: bool) -> list[dict]:
    """Para cada persona/organización más mencionada del día, un artículo
    REAL representativo (link + dominio). El criterio principal es el
    OFFSET de caracter de la mención (V2Persons/V2Organizations traen
    'Nombre,offset') -- una mención cerca del inicio del texto suele
    significar que el artículo es SOBRE esa persona/tema, no una mención de
    paso; sin esto, para un nombre tan omnipresente como "Trump" se elegía
    cualquier artículo que lo mencionara en cualquier parte, y el
    resultado no informaba nada real sobre él (caso reportado: el titular
    elegido ni siquiera trataba de Trump). Medio de referencia y fecha
    quedan como criterio de desempate, no principal. El GKG no trae título
    de artículo (ver alerts/gdelt_bigquery.py docstring) -- el título real
    se baja aparte con _fetch_real_title, igual que para resultados de una
    búsqueda."""
    from google.cloud import bigquery
    from alerts.gdelt import SERIOUS_DOMAINS
    domain_filter = "AND SourceCommonName LIKE '%.mx'" if mx_only else ""
    sql = f"""
        WITH base AS (
          SELECT DocumentIdentifier, SourceCommonName, DATE, V2Persons, V2Organizations
          FROM `{GKG_TABLE}`
          WHERE DATE(_PARTITIONTIME) = @day AND {LANG_COND} {domain_filter}
        ),
        persons AS (
          SELECT DocumentIdentifier, SourceCommonName, DATE,
                 TRIM(SPLIT(p, ',')[OFFSET(0)]) AS name,
                 MIN(SAFE_CAST(SPLIT(p, ',')[SAFE_OFFSET(1)] AS INT64)) AS min_offset
          FROM base, UNNEST(SPLIT(V2Persons, ';')) AS p
          WHERE p != '' AND LOWER(TRIM(SPLIT(p, ',')[OFFSET(0)])) NOT IN UNNEST(@stoplist)
          GROUP BY DocumentIdentifier, SourceCommonName, DATE, name
        ),
        orgs AS (
          SELECT DocumentIdentifier, SourceCommonName, DATE,
                 TRIM(SPLIT(o, ',')[OFFSET(0)]) AS name,
                 MIN(SAFE_CAST(SPLIT(o, ',')[SAFE_OFFSET(1)] AS INT64)) AS min_offset
          FROM base, UNNEST(SPLIT(V2Organizations, ';')) AS o
          WHERE o != '' AND LOWER(TRIM(SPLIT(o, ',')[OFFSET(0)])) NOT IN UNNEST(@stoplist)
          GROUP BY DocumentIdentifier, SourceCommonName, DATE, name
        ),
        combined AS (
          SELECT 'person' AS kind, name, DocumentIdentifier, SourceCommonName, DATE, min_offset FROM persons WHERE name != ''
          UNION ALL
          SELECT 'org' AS kind, name, DocumentIdentifier, SourceCommonName, DATE, min_offset FROM orgs WHERE name != ''
        ),
        -- Top N nombres COMBINADO (personas + organizaciones juntas, no un
        -- top separado por tipo) -- para armar un solo "top 10 noticias",
        -- no dos rankings de 15 cada uno. GROUP BY (no SELECT DISTINCT)
        -- a propósito: QUALIFY sobre un SELECT DISTINCT rankea ANTES de
        -- dedupear (ROW_NUMBER corre sobre las filas de documento
        -- originales, no sobre nombres únicos) -- confirmado con datos
        -- reales: un solo nombre con miles de documentos acaparaba todo
        -- el ranking. GROUP BY sí produce una fila real por nombre antes
        -- de que QUALIFY la rankee.
        top_names AS (
          SELECT kind, name, COUNT(*) AS mentions
          FROM combined
          GROUP BY kind, name
          QUALIFY ROW_NUMBER() OVER (ORDER BY COUNT(*) DESC) <= @top_n
        )
        SELECT c.kind, c.name, t.mentions,
               c.DocumentIdentifier AS link, c.SourceCommonName AS domain,
               ROW_NUMBER() OVER (
                 PARTITION BY c.kind, c.name
                 ORDER BY IFNULL(c.min_offset, 999999999) ASC,
                          CASE WHEN c.SourceCommonName IN UNNEST(@serious) THEN 0 ELSE 1 END, c.DATE DESC
               ) AS candidate_rank
        FROM combined c
        JOIN top_names t USING (kind, name)
        QUALIFY candidate_rank <= @candidates_per_story
        ORDER BY t.mentions DESC, c.kind, c.name, candidate_rank
    """
    params = [
        bigquery.ScalarQueryParameter('day', 'DATE', day),
        bigquery.ScalarQueryParameter('top_n', 'INT64', TOP_STORIES_N),
        bigquery.ScalarQueryParameter('candidates_per_story', 'INT64', CANDIDATES_PER_STORY),
        bigquery.ArrayQueryParameter('serious', 'STRING', sorted(SERIOUS_DOMAINS)),
        bigquery.ArrayQueryParameter('stoplist', 'STRING', sorted(ENTITY_STOPLIST)),
    ]
    rows = _run(client, sql, params)
    # Varios candidatos por tema, en orden de preferencia -- ver get_pulse:
    # si el mejor candidato bloquea la descarga del título real (403, sitio
    # caído, timeout), se prueba con el siguiente en vez de mostrar solo el
    # nombre de la entidad sin ningún contexto (caso real: lamag.com
    # devolviendo 403 para "Dario Amodei").
    by_story: dict[tuple, list[dict]] = {}
    for r in rows:
        key = (r['kind'], r['name'])
        by_story.setdefault(key, {'mentions': r['mentions'], 'candidates': []})
        by_story[key]['candidates'].append({'link': r['link'], 'domain': r['domain']})
    out = [{'kind': k[0], 'name': k[1], 'mentions': v['mentions'], 'candidates': v['candidates']}
           for k, v in by_story.items()]
    out.sort(key=lambda s: -s['mentions'])
    return out


def _query_locations(client, day: str, mx_only: bool) -> dict:
    """Lugares geográficos más mencionados -- V2Locations trae lat/lon
    directo (ver alerts/gdelt_bigquery.py docstring de extra_data)."""
    from google.cloud import bigquery
    domain_filter = "AND SourceCommonName LIKE '%.mx'" if mx_only else ""
    sql = f"""
        WITH base AS (
          SELECT DocumentIdentifier, V2Locations
          FROM `{GKG_TABLE}`
          WHERE DATE(_PARTITIONTIME) = @day AND {LANG_COND} {domain_filter}
        ),
        locs AS (
          SELECT DISTINCT DocumentIdentifier,
                 SPLIT(loc, '#')[SAFE_OFFSET(1)] AS name,
                 SAFE_CAST(SPLIT(loc, '#')[SAFE_OFFSET(5)] AS FLOAT64) AS lat,
                 SAFE_CAST(SPLIT(loc, '#')[SAFE_OFFSET(6)] AS FLOAT64) AS lon
          FROM base, UNNEST(SPLIT(V2Locations, ';')) AS loc WHERE loc != ''
        )
        SELECT name, ANY_VALUE(lat) AS lat, ANY_VALUE(lon) AS lon, COUNT(*) AS mentions
        FROM locs
        WHERE name IS NOT NULL AND name != '' AND lat IS NOT NULL AND lon IS NOT NULL
        GROUP BY name
        ORDER BY mentions DESC
        LIMIT @top_n
    """
    params = [
        bigquery.ScalarQueryParameter('day', 'DATE', day),
        bigquery.ScalarQueryParameter('top_n', 'INT64', TOP_LOCATIONS_N),
    ]
    rows = _run(client, sql, params)
    return {
        'text': [f'{r["name"]} ({r["mentions"]} menci{"ón" if r["mentions"] == 1 else "ones"})' for r in rows],
        'lat':  [r['lat'] for r in rows],
        'lon':  [r['lon'] for r in rows],
        'size': [r['mentions'] for r in rows],
    }


def _query_avg_tone(client, day: str, mx_only: bool) -> dict:
    from google.cloud import bigquery
    domain_filter = "AND SourceCommonName LIKE '%.mx'" if mx_only else ""
    sql = f"""
        SELECT AVG(SAFE_CAST(SPLIT(V2Tone, ',')[SAFE_OFFSET(0)] AS FLOAT64)) AS avg_tone,
               COUNT(*) AS n
        FROM `{GKG_TABLE}`
        WHERE DATE(_PARTITIONTIME) = @day AND {LANG_COND} {domain_filter}
    """
    params = [bigquery.ScalarQueryParameter('day', 'DATE', day)]
    rows = _run(client, sql, params)
    if not rows or rows[0]['avg_tone'] is None:
        return {'avg': None, 'n': 0}
    return {'avg': round(rows[0]['avg_tone'], 2), 'n': rows[0]['n']}


def get_pulse(scope: str = 'world', day: str | None = None, force: bool = False) -> dict:
    """scope: 'world' o 'mx'. day: 'YYYY-MM-DD' en UTC (el DATE del GKG es
    UTC, ver alerts/gdelt_bigquery.py); por default, hoy."""
    day = day or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if not force:
        cached = _cache_get(day, scope)
        if cached is not None:
            return cached

    client = _client()
    if not client:
        return {'available': False, 'day': day}

    mx_only   = (scope == 'mx')
    entities  = _query_top_entities(client, day, mx_only)
    raw_stories = _query_top_stories(client, day, mx_only)
    locations = _query_locations(client, day, mx_only)
    tone      = _query_avg_tone(client, day, mx_only)

    # Títulos reales en paralelo -- mismo mecanismo que alerts/
    # gdelt_bigquery.py:fetch_articles (el GKG no trae título de artículo).
    # Se bajan TODOS los candidatos de una sola vez (aplanados) para no
    # perder el paralelismo yendo tema por tema.
    from concurrent.futures import ThreadPoolExecutor
    from alerts.gdelt_bigquery import _fetch_real_title, TITLE_FETCH_WORKERS
    all_links = [c['link'] for s in raw_stories for c in s['candidates']]
    with ThreadPoolExecutor(max_workers=TITLE_FETCH_WORKERS) as pool:
        all_titles = list(pool.map(_fetch_real_title, all_links))
    title_by_link = dict(zip(all_links, all_titles))

    stories, seen_links = [], set()
    for s in raw_stories:
        # El mejor candidato puede bloquear la descarga (403, timeout, sitio
        # caído) -- caso real: lamag.com devolvía 403 para "Dario Amodei" y
        # se mostraba solo el nombre, sin ningún contexto. Se prueba cada
        # candidato en orden hasta encontrar uno con título real.
        chosen = None
        for c in s['candidates']:
            if c['link'] in seen_links:
                continue
            title = title_by_link.get(c['link'])
            if title:
                chosen = {'title': title, 'link': c['link'], 'domain': c['domain']}
                break
        if chosen is None:
            # Ningún candidato dio título real -- se muestra igual con el
            # primer link disponible (sigue siendo un artículo real al que
            # ir), el título visible cae al nombre de la entidad.
            fallback = next((c for c in s['candidates'] if c['link'] not in seen_links), None)
            if fallback is None:
                continue
            chosen = {'title': s['name'], 'link': fallback['link'], 'domain': fallback['domain']}
        seen_links.add(chosen['link'])
        stories.append({'name': s['name'], 'mentions': s['mentions'], **chosen})

    now = datetime.now()
    data = {'available': True, 'day': day, 'scope': scope,
            'stories': stories,
            'persons': entities['persons'], 'orgs': entities['orgs'],
            'locations': locations, 'tone': tone,
            # Guardados DENTRO del blob (no solo en la columna computed_at
            # de la tabla) para que sobrevivan el viaje de ida y vuelta por
            # _cache_get sin tocar su firma -- ver plantilla world_pulse.html.
            'computed_at': now.strftime('%Y-%m-%d %H:%M'),
            'next_update': (now + timedelta(minutes=REFRESH_MIN)).strftime('%Y-%m-%d %H:%M')}
    _cache_set(day, scope, data)
    return data
