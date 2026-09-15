"""
alerts/gdelt.py — Búsqueda de noticias vía GDELT Project (DOC 2.0 API,
https://api.gdeltproject.org/api/v2/doc/doc), sin API key. Complementa a
alerts/googlenews.py con cobertura internacional. Se acota solo por idioma
(inglés/español, ver LANGUAGE_FILTER) -- NO se restringe por fuente al
guardar: se probó limitar a medios "serios" (SERIOUS_DOMAINS) desde el
fetch, pero eso también tapaba cobertura real que puede ser valiosa (ej.
medios de África/Asia hablando de México). En vez de eso, cada artículo se
guarda con su channel_id según sea nacional/internacional (ver
GDELT_CHANNEL_ID/GDELT_MX_CHANNEL_ID en alerts/channel_types.py), y
SERIOUS_DOMAINS queda disponible como filtro OPCIONAL sobre los resultados
ya guardados (ver alerts/channel_types.py MEDIA_TYPE_SUBFILTERS
"gdelt_serious" y alerts/app.py:_match_where) -- lo activas cuando te
conviene, sin perder lo demás. Es una fuente opt-in aparte ("gdelt" en
media_types), no un reemplazo de Google Noticias.

Mismo problema de fondo que Google Noticias -- el endpoint tiene un tope de
resultados por consulta (250, mayor al de Google pero real) -- y misma
solución: fetch_articles_range bisecta el rango recursivamente cuando se
topa. A diferencia de Google, GDELT sí entiende fecha+hora en startdatetime/
enddatetime, pero se bisecta solo hasta el día por consistencia con
googlenews.py y para no multiplicar aún más las consultas (rate limit propio
de GDELT: 1 consulta cada 5s, ver _RATE_LIMIT_SEC).

Nota: como Google Noticias, es un endpoint público sin SLA -- si GDELT
cambia el formato o límites, fetch_articles() empieza a devolver listas
vacías (logueado como error), sin tumbar el resto del watcher.
"""
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from alerts.channel_types import GDELT_CHANNEL_ID, GDELT_MX_CHANNEL_ID

logger = logging.getLogger('gdelt')

DOC_API_URL    = 'https://api.gdeltproject.org/api/v2/doc/doc'
TIMEOUT        = 20
GDELT_CAP      = 250   # tope real observado del endpoint por consulta
_RATE_LIMIT_SEC = 5.0  # GDELT pide max 1 consulta cada 5s (429 si se excede)

# Idioma: solo inglés/español (sourcelang:) -- los dos que puede leer quien
# revisa el reporte. Esto SÍ va en la consulta a GDELT.
LANGUAGE_FILTER = '(sourcelang:english OR sourcelang:spanish)'

# Lista de medios/agencias internacionales de referencia -- NO se usa para
# excluir nada al guardar (ver docstring del módulo), solo como filtro
# opcional "gdelt_serious" sobre resultados ya guardados (alerts/app.py:
# _match_where). No incluye prensa mexicana a propósito: ese filtro nunca
# aplica a lo clasificado como nacional (ver GDELT_MX_CHANNEL_ID), que
# siempre se puede ver completo sin restricción de lista.
SERIOUS_DOMAINS = {
    # Agencias de noticias
    'reuters.com', 'apnews.com', 'efe.com', 'afp.com', 'xinhuanet.com',
    'tass.com', 'ansa.it', 'dpa-international.com', 'kyodonews.net',
    'upi.com',
    # Prensa internacional en inglés
    'bbc.com', 'aljazeera.com', 'theguardian.com', 'nytimes.com',
    'washingtonpost.com', 'ft.com', 'economist.com', 'cnn.com',
    'bloomberg.com', 'wsj.com', 'npr.org', 'time.com', 'newsweek.com',
    'usatoday.com', 'independent.co.uk', 'telegraph.co.uk',
    # Prensa internacional en español / bilingüe (no mexicana -- lo
    # mexicano ya cae en "nacional", ver GDELT_MX_CHANNEL_ID)
    'dw.com', 'france24.com', 'elpais.com', 'infobae.com', 'elmundo.es',
    'abc.es', 'lavanguardia.com', 'elperiodico.com',
    'lanacion.com.ar', 'clarin.com',
    'latercera.com', 'emol.com',
    'eltiempo.com', 'elespectador.com', 'elcomercio.pe', 'elpais.com.uy',
}


def _is_mexican(art: dict) -> bool:
    """Usa el país de la fuente que GDELT ya trae (sourcecountry) en vez de
    adivinar por dominio -- muchos medios mexicanos reales no usan .mx
    (ej. sdpnoticias.com, diarioportal.com), así que un chequeo de TLD se
    quedaría corto. Fallback a .mx solo si GDELT no trae el campo."""
    country = (art.get('sourcecountry') or '').strip().lower()
    if country:
        return country == 'mexico'
    return (art.get('domain') or '').strip().lower().endswith('.mx')

_last_request_ts = 0.0


def _throttle():
    """Espera lo necesario para no exceder 1 consulta cada _RATE_LIMIT_SEC
    -- global al proceso, no por keyword, porque el límite es del endpoint,
    no de la búsqueda."""
    global _last_request_ts
    wait = _RATE_LIMIT_SEC - (time.time() - _last_request_ts)
    if wait > 0:
        time.sleep(wait)
    _last_request_ts = time.time()


def fetch_articles(query: str, date_from: str | None = None, date_to: str | None = None,
                    _retry: bool = True) -> list[dict]:
    """Devuelve [{title, link, source, source_domain, published}] -- mismo
    shape que alerts.googlenews.fetch_articles, para reusar el mismo código
    de guardado en alerts/watcher.py. date_from/date_to en 'YYYY-MM-DD'
    (inclusive); sin fechas trae lo más reciente (ventana corta, GDELT
    limita a mostrar solo cobertura de los últimos meses sin fecha).

    _retry: uso interno -- si GDELT responde 429 (tope de tasa propio, ver
    _RATE_LIMIT_SEC), se reintenta UNA vez tras esperar. Sin esto, un 429
    devolvía lista vacía silenciosamente -- indistinguible de "sin noticias
    ese día" para quien ve el resultado (confirmado: pasó de verdad con una
    búsqueda real de "sheinbaum"/"méxico" mientras se probaba este módulo a
    fondo el mismo día)."""
    full_query = f'{query} {LANGUAGE_FILTER}'
    params = {
        'query':      full_query,
        'mode':       'artlist',
        'maxrecords': str(GDELT_CAP),
        'sort':       'datedesc',
        'format':     'json',
    }
    if date_from:
        params['startdatetime'] = date_from.replace('-', '') + '000000'
    if date_to:
        end = (datetime.strptime(date_to, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y%m%d')
        params['enddatetime'] = end + '000000'

    url = f'{DOC_API_URL}?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    _throttle()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 429 and _retry:
            logger.warning(f"GDELT: 429 (tope de tasa) para '{query}', reintentando en {_RATE_LIMIT_SEC + 1}s...")
            time.sleep(_RATE_LIMIT_SEC + 1)
            return fetch_articles(query, date_from, date_to, _retry=False)
        logger.error(f"GDELT: error {e.code} consultando '{query}': {e.read()[:200]}")
        return []
    except urllib.error.URLError as e:
        logger.error(f"GDELT: error consultando '{query}': {e}")
        return []
    except Exception as e:
        logger.error(f"GDELT: error inesperado consultando '{query}': {e}")
        return []

    try:
        data = json.loads(raw)
    except Exception as e:
        logger.error(f"GDELT: error parseando JSON para '{query}': {e}")
        return []

    out = []
    for art in data.get('articles', []):
        title = (art.get('title') or '').strip()
        link  = (art.get('url') or '').strip()
        domain = (art.get('domain') or '').strip() or None
        seen  = art.get('seendate') or ''  # formato YYYYMMDDTHHMMSSZ (UTC)
        if not title or not link or not seen:
            continue
        # No se descarta nada por dominio aquí -- se probó restringir a
        # SERIOUS_DOMAINS en el fetch, pero eso también tapaba cobertura
        # real y a veces interesante (ej. medios de África/Asia hablando de
        # México) que vale la pena conservar. SERIOUS_DOMAINS se usa como
        # filtro OPCIONAL sobre datos ya guardados (ver
        # alerts/channel_types.py MEDIA_TYPE_SUBFILTERS "gdelt_serious" y
        # alerts/app.py:_match_where), no como recorte al guardar.
        try:
            # 'Z' = UTC -- sin convertir a hora local quedaba ~6h adelantado
            # frente a TV/radio/Google Noticias (que sí lo hacen, ver
            # alerts/googlenews.py), descuadrando el orden cronológico y el
            # mapa de calor al mezclarse en la misma tabla `matches`.
            pub_dt = datetime.strptime(seen, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
            pub_dt = pub_dt.astimezone().replace(tzinfo=None)
        except Exception:
            continue
        out.append({
            'title':         title,
            'link':          link,
            'source':        domain or 'GDELT',
            'source_domain': domain,
            'published':     pub_dt,
            # channel_id explícito (no el default que use el caller) --
            # separa nacional/internacional para poder filtrarlos aparte en
            # los resultados (ver alerts/watcher.py:_poll_articles_for_search
            # y alerts/channel_types.py MEDIA_TYPE_SUBFILTERS).
            'channel_id':    GDELT_MX_CHANNEL_ID if _is_mexican(art) else GDELT_CHANNEL_ID,
        })
    return out


def fetch_articles_range(query: str, date_from: str, date_to: str) -> list[dict]:
    """Como fetch_articles, pero bisecta el rango (hasta granularidad de un
    día) si la respuesta viene al tope real de GDELT -- ver el docstring del
    módulo y alerts/googlenews.py:fetch_articles_range (misma lógica)."""
    articles = fetch_articles(query, date_from=date_from, date_to=date_to)
    if len(articles) < GDELT_CAP or date_from == date_to:
        return articles
    d0  = datetime.strptime(date_from, '%Y-%m-%d').date()
    d1  = datetime.strptime(date_to,   '%Y-%m-%d').date()
    mid = d0 + (d1 - d0) // 2
    left  = fetch_articles_range(query, date_from, mid.isoformat())
    right = fetch_articles_range(query, (mid + timedelta(days=1)).isoformat(), date_to)
    return left + right
