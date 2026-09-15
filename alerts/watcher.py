"""
alerts/watcher.py — Hilo de fondo que monitorea transcripciones y dispara alertas.
Corre cada POLL_INTERVAL segundos. Lee transcriptions.db, cruza contra búsquedas
activas, guarda coincidencias y dispara correos según el modo de entrega.
"""
import html
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
import logging
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, date, timedelta
from pathlib  import Path

from alerts.mailer         import send_immediate, send_daily_report, send_final_report
from alerts.telegram       import notify_match as tg_notify_match, send_telegram as tg_send_telegram
from alerts.epg            import refresh_if_needed as epg_refresh, ensure_schema as epg_schema
from alerts.channel_types  import channel_type, parse_media_types, NEWS_CHANNEL_ID, YOUTUBE_CHANNEL_ID, GDELT_CHANNEL_ID
from alerts.googlenews     import fetch_articles as gnews_fetch, fetch_articles_range
from alerts.gdelt          import fetch_articles as gdelt_fetch, fetch_articles_range as gdelt_fetch_range

logger = logging.getLogger('watcher')

BASE_DIR      = Path(__file__).parent.parent
ALERTS_DB     = BASE_DIR / 'alerts.db'
TRANS_DB      = BASE_DIR / 'transcriptions.db'
POLL_INTERVAL = 5   # segundos entre cada ciclo
NEWS_POLL_MINUTES  = 30  # cada cuánto se vuelve a consultar Google Noticias por búsqueda activa
GDELT_POLL_MINUTES = 120  # ídem para GDELT (fuente opt-in, ver alerts/channel_types.py) --
                          # más espaciado que Google Noticias a propósito: BigQuery cobra
                          # por partición de día escaneada (ver _poll_gdelt_for_search), así
                          # que consultar cada 30 min no traía más noticias, solo repetía
                          # el escaneo del mismo día varias veces de más.
WEEKLY_REPORT_WEEKDAY = 0  # lunes -- reporta la semana calendario Lun-Dom recién cerrada
# YouTube Data API v3 tiene cuota diaria limitada (10,000 unidades/día,
# 100 por búsqueda -- ~100 búsquedas/día en total, compartidas entre TODAS
# las búsquedas activas con YouTube habilitado). A diferencia de Google
# Noticias, consultar cada 20 min agotaría la cuota rápido con pocas
# búsquedas activas. Política: una consulta inmediata al crear/activar la
# búsqueda (youtube_last_fetch NULL) + una consulta diaria a las
# YOUTUBE_DAILY_HOUR (hora local) para cubrir lo subido durante el día.
YOUTUBE_DAILY_HOUR = 1  # 1am -- corre en su propio hilo (_youtube_loop) para
                        # no frenar el loop rápido de 5s con descargas/transcripciones.

# El histórico de una búsqueda nueva (ver _process, sección 1) se procesaba
# en un solo hilo -- por el GIL de Python, el emparejamiento de texto
# (regex/fonética por fila) nunca usaba más de UN núcleo aunque la máquina
# tenga 32, sin importar cuántos días o registros haya que revisar. Se
# paraleliza por PROCESO (no hilo, para sí aprovechar varios núcleos de
# verdad) partiendo el trabajo por canal -- cada canal es independiente para
# el dedup por ventana de tiempo (_recent_match_exists, la llave incluye
# channel_id), así que procesarlos en paralelo no rompe esa garantía,
# siempre que CADA canal se siga procesando en orden cronológico dentro de
# su propio worker (ver _backfill_channel).
#
# Este es el tope del pool COMPARTIDO entre todas las búsquedas nuevas del
# ciclo (ver _process, sección 1) -- no un tope por búsqueda. Si solo hay
# una búsqueda nueva, sus tareas son las únicas en la cola y de facto se
# queda con todo el pool; si hay varias a la vez, sus tareas (una por canal)
# se mezclan en la misma cola y se reparten solas según van terminando.
#
# Tope conservador, NO os.cpu_count(): esta misma máquina corre 24/7 la
# transcripción en vivo de TV/radio (GPU + ffmpeg, ver transcriber_ctc_es.py/
# transcriber_parakeet.py) y esos NUNCA deben quedarse sin CPU por una
# búsqueda histórica -- más info en el incidente de OOM documentado ahí.
# Configurable por si la carga típica de la máquina cambia.
MAX_BACKFILL_WORKERS = int(os.environ.get("TRANSCRIBER_BACKFILL_WORKERS", "12"))


# ── Normalización fonética española ──────────────────────────────────────────
def _strip_accents(text: str) -> str:
    text = unicodedata.normalize('NFD', text.lower())
    return ''.join(c for c in text if unicodedata.category(c) != 'Mn')

def _phonetic_es(text: str) -> str:
    """Normalización fonética básica del español."""
    t = _strip_accents(text)
    t = re.sub(r'\bh', '', t)          # h inicial (muda)
    # "sh" no existe en español -- el ASR lo transcribe de forma inconsistente
    # en nombres extranjeros (Sheinbaum/Scheinbaum, Shakira/Chakira, etc.),
    # a veces como "sh" y a veces insertando una "c" ("sch"). Sin esto, buscar
    # "sheinbaum" en modo fonético no encontraba las menciones transcritas
    # como "Scheinbaum" -- ambas colapsan a la misma forma ("seinbaum").
    t = t.replace('sch', 's')
    t = t.replace('sh', 's')
    t = re.sub(r'n(?=[bmp])', 'm', t)  # asimilación nasal real del español
                                        # ("un beso"~"um beso", "envidia"~"embidia")
    t = t.replace('v', 'b')            # b/v
    t = t.replace('ll', 'y')           # ll → y
    t = re.sub(r'qu([ei])', r'k\1', t) # que/qui → ke/ki
    t = re.sub(r'c([ei])', r's\1', t)  # ce/ci → se/si
    t = re.sub(r'g([ei])', r'j\1', t)  # ge/gi → je/ji
    t = t.replace('z', 's')            # z → s
    t = t.replace('ck', 'k')           # ck → k
    t = re.sub(r'x', 'ks', t)          # x → ks
    return t

def _match(text: str, keyword: str, phonetic: bool, whole_word: bool = False) -> bool:
    """whole_word exige que la keyword aparezca delimitada por separadores de
    palabra (no dentro de una palabra compuesta, ej. "día" no debe casar con
    "diálogo" ni "mediodía"). Se comprueba con límites \\w sobre el mismo
    texto normalizado en ambos lados (keyword y texto), así que es seguro
    aunque la normalización fonética cambie longitudes de palabra.

    keyword puede ser compuesta -- varios términos unidos con "+" (ej.
    "homicidio+juan") que deben aparecer TODOS en el mismo texto, en
    cualquier orden y sin necesidad de estar juntos -- a diferencia de una
    keyword normal de varias palabras ("claudia sheinbaum"), que sí exige
    la frase exacta. Cada término se evalúa por separado con las mismas
    reglas (phonetic/whole_word) y se exige que TODOS matcheen."""
    if '+' in keyword:
        terms = [t.strip() for t in keyword.split('+') if t.strip()]
        return bool(terms) and all(_match(text, t, phonetic, whole_word) for t in terms)
    norm_text = _phonetic_es(text)    if phonetic else _strip_accents(text)
    norm_kw   = _phonetic_es(keyword) if phonetic else _strip_accents(keyword)
    if not norm_kw:
        return False
    if whole_word:
        return re.search(r'(?<!\w)' + re.escape(norm_kw) + r'(?!\w)', norm_text) is not None
    return norm_kw in norm_text


def _excluded(text: str, exclude_words: list[str], phonetic: bool, whole_word: bool) -> bool:
    """True si alguna palabra de exclusión aparece en el MISMO texto que
    disparó el match -- ej. buscar "rocha" (sin whole_word, o incluso con
    phonetic) excluyendo "reprochar"/"derrochar" (que contienen "rocha"
    como substring). Reusa _match con las mismas opciones de la búsqueda
    para que la exclusión sea consistente con cómo se detectó el match."""
    if not exclude_words:
        return False
    return any(_match(text, ex, phonetic, whole_word) for ex in exclude_words)


DEDUP_WINDOW_SEC = 60  # "1 minuto de espacio por canal" -- ver dedup_channel en searches

def _recent_match_exists(adb, search_id: int, keyword: str, channel_id: int, timestamp: str) -> bool:
    """True si ya hay una coincidencia guardada para esta misma búsqueda +
    keyword + canal en el último DEDUP_WINDOW_SEC antes de `timestamp`. Se
    usa para que una palabra repetida varias veces en la misma nota/segmento
    (varios chunks de 30s seguidos mencionándola) cuente como una sola
    coincidencia, no una por cada chunk. Se procesan los chunks en orden
    cronológico, así que cualquier coincidencia previa dentro de la ventana
    ya está guardada al momento de esta consulta."""
    row = adb.execute("""
        SELECT 1 FROM matches
        WHERE search_id=? AND keyword=? AND channel_id=?
          AND timestamp > datetime(?, ?) AND timestamp <= ?
        LIMIT 1
    """, (search_id, keyword, channel_id, timestamp, f'-{DEDUP_WINDOW_SEC} seconds', timestamp)).fetchone()
    return row is not None


def _check_threshold_alert(adb, s, cfg, smtp) -> None:
    """Cuenta coincidencias de esta búsqueda en los últimos
    threshold_window_min minutos; si cruza threshold_count, dispara UNA
    alerta (no una por cada match subsecuente) -- cooldown = mismo
    threshold_window_min desde la última alerta (evita re-alertar en cada
    coincidencia nueva mientras la racha sigue activa). `s` ya trae
    u_tg_chat_id (viene del JOIN a users en la query de búsquedas activas
    de _process, mismo patrón que el resto de las alertas de Telegram)."""
    window    = int(s['threshold_window_min'] or 30)
    threshold = int(s['threshold_count'] or 5)
    count = adb.execute("""
        SELECT COUNT(*) c FROM matches
        WHERE search_id=? AND found_at >= datetime('now','localtime',?)
    """, (s['id'], f'-{window} minutes')).fetchone()['c']
    if count < threshold:
        return
    last = s['last_threshold_alert'] if 'last_threshold_alert' in s.keys() else None
    if last:
        try:
            last_dt = datetime.strptime(last[:19], '%Y-%m-%d %H:%M:%S')
            if (datetime.now() - last_dt).total_seconds() < window * 60:
                return  # cooldown activo -- ya se alertó por esta racha
        except ValueError:
            pass

    tg_token = (cfg or {}).get('tg_token', '')
    tg_chat  = (s['u_tg_chat_id'] if 'u_tg_chat_id' in s.keys() else '') or (cfg or {}).get('tg_chat_id', '')
    if s['notify_telegram'] and tg_token and tg_chat:
        msg = (f"<b>⚠️ Alerta de frecuencia — {html.escape(s['name'])}</b>\n"
               f"{count} coincidencias en los últimos {window} min (umbral: {threshold}).")
        try:
            tg_send_telegram(tg_token, tg_chat, msg)
        except Exception as e:
            logger.error(f"[TG] threshold alert error search {s['id']}: {e}")
    if smtp and s['report_email']:
        try:
            from alerts.mailer import send_threshold_alert
            send_threshold_alert(dict(s), count, window, threshold, smtp)
        except Exception as e:
            logger.error(f"Threshold alert email error search {s['id']}: {e}")

    adb.execute("UPDATE searches SET last_threshold_alert=? WHERE id=?",
                (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), s['id']))
    adb.commit()


# ── Conexiones ────────────────────────────────────────────────────────────────
# cache_size/mmap_size más grandes que el default -- el watcher escanea
# transcriptions.db en un loop constante (POLL_INTERVAL), y ahora compite con
# hasta 20 clientes del dashboard por el mismo archivo.
_TUNE_PRAGMAS = (
    "PRAGMA synchronous=NORMAL",
    "PRAGMA cache_size=-64000",
    "PRAGMA mmap_size=268435456",
)

def _adb():
    c = sqlite3.connect(str(ALERTS_DB), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    for p in _TUNE_PRAGMAS:
        c.execute(p)
    return c

def _tdb():
    c = sqlite3.connect(str(TRANS_DB), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    for p in _TUNE_PRAGMAS:
        c.execute(p)
    return c


# ── Helpers de estado ─────────────────────────────────────────────────────────
def _get_setting(adb, key: str, default='0') -> str:
    r = adb.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r['value'] if r else default

def _set_setting(adb, key: str, value: str):
    adb.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))

def _smtp_cfg(adb) -> dict | None:
    rows = adb.execute("SELECT key, value FROM settings").fetchall()
    cfg  = {r['key']: r['value'] for r in rows}
    return cfg if (cfg.get('smtp_host') or cfg.get('gmail_refresh_token')) else None

def _full_cfg(adb) -> dict:
    rows = adb.execute("SELECT key, value FROM settings").fetchall()
    return {r['key']: r['value'] for r in rows}


# ── Contexto del match ────────────────────────────────────────────────────────
CONTEXT_MAXLEN = 1500  # antes 500 (un solo chunk) -- ahora hasta 3 pegados (previo+actual+siguiente)

# Separadores invisibles entre el chunk original y los chunks vecinos que se
# le pegan para dar más contexto -- marcan dónde empieza/termina el chunk de
# 30s donde SIEMPRE está la keyword real (el match se detecta contra el
# chunk original antes de pegar nada). alerts/app.py los usa para seguir
# calculando precise_timestamp sobre la duración real de ESE chunk, no sobre
# el texto ya con contexto pegado -- si no, el cálculo "en qué segundo se
# dijo la palabra" se distorsiona para keywords cerca del inicio/final del
# chunk (justo los casos que esto arregla). Deben coincidir con
# CHUNK_SEP_PREV / CHUNK_SEP_NEXT en alerts/app.py. Dos marcadores distintos
# (no uno solo) para poder distinguir sin ambigüedad cuál es el chunk
# original aunque falte el previo o el siguiente.
CHUNK_SEP_PREV = '⁠'   # WORD JOINER (U+2060) -- antes del chunk actual
CHUNK_SEP_NEXT = '​'   # ZERO WIDTH SPACE (U+200B) -- después del chunk actual

def _with_context_chunks(tdb, channel_id: int, timestamp: str, text: str) -> str:
    """Concatena los chunks ANTERIOR y SIGUIENTE del mismo canal (los que ya
    existan) al del chunk donde se detectó la keyword. Un chunk es ~30s de
    audio, así que si la keyword cae cerca del inicio o del final, casi no
    queda contexto de ese lado dentro del mismo chunk -- el resto de la
    frase/anuncio está en el chunk vecino, una fila aparte en transcriptions.
    Esto puede triplicar la ventana de texto capturada (30s -> hasta ~90s)
    sin tocar la captura/transcripción en vivo, reusando datos que ya se
    están grabando. Usa el índice idx_trans_channel_ts (channel_id, timestamp)."""
    prev = tdb.execute("""
        SELECT text FROM transcriptions
        WHERE channel_id=? AND timestamp < ?
        ORDER BY timestamp DESC LIMIT 1
    """, (channel_id, timestamp)).fetchone()
    nxt = tdb.execute("""
        SELECT text FROM transcriptions
        WHERE channel_id=? AND timestamp > ?
        ORDER BY timestamp ASC LIMIT 1
    """, (channel_id, timestamp)).fetchone()

    result = text
    if nxt and nxt['text'] and nxt['text'] != '[~]':
        result = f"{result} {CHUNK_SEP_NEXT} {nxt['text']}"
    if prev and prev['text'] and prev['text'] != '[~]':
        result = f"{prev['text']} {CHUNK_SEP_PREV} {result}"
    return result[:CONTEXT_MAXLEN]


# ── Google Noticias ───────────────────────────────────────────────────────────
def _poll_articles_for_search(adb, s, keywords: list[str], exclude_words: list[str] | None,
                               date_from: str | None, date_to: str | None,
                               fetch_one, fetch_range, channel_id: int) -> int:
    """Lógica compartida entre Google Noticias y GDELT (ambas fuentes son
    "keyword -> lista de artículos", ver alerts/googlenews.py y
    alerts/gdelt.py -- mismo shape de artículo, mismo problema de tope por
    consulta con sesgo a lo reciente, misma solución de bisección). Guarda
    los artículos nuevos como matches (channel_name=fuente real del
    artículo). date_from/date_to acotan (fetch histórico inicial); sin
    fecha trae lo más reciente. exclude_words descarta el artículo si su
    título contiene alguna palabra excluida.

    Cada keyword dispara su PROPIA búsqueda (fetch_one/fetch_range se llama
    una vez por keyword) -- si la búsqueda tiene varias keywords que se
    solapan (ej. "rocha", "rocha moya"), el MISMO artículo real aparece en
    más de un resultado. Se dedupea por link Y por (título, fuente) -- el
    link de estas fuentes puede ser una URL de redirección que varía entre
    una búsqueda y otra para el MISMO artículo real, así que el link solo
    no basta. A diferencia de TV/radio, aquí no depende de dedup_channel:
    es siempre el mismo artículo, nunca hay razón legítima de guardarlo dos
    veces."""
    phonetic   = bool(s['phonetic'])
    whole_word = bool(s['whole_word'])
    total = 0
    seen_links  = set()
    seen_titles = set()

    for kw in keywords:
        if date_from and date_to and date_from != date_to:
            articles = fetch_range(kw, date_from, date_to)
        else:
            articles = fetch_one(kw, date_from=date_from, date_to=date_to)
        for art in articles:
            title_key = (art['title'], art['source'])
            if art['link'] in seen_links or title_key in seen_titles:
                continue
            # Además de los sets en memoria (dedup dentro de esta corrida),
            # se checa la base -- un poll anterior ya pudo haber guardado
            # este mismo artículo bajo otra keyword.
            if adb.execute("""SELECT 1 FROM matches
                WHERE search_id=? AND (source_url=? OR (matched_text=? AND channel_name=?))""",
                (s['id'], art['link'], art['title'], art['source'])).fetchone():
                seen_links.add(art['link'])
                seen_titles.add(title_key)
                continue
            if _excluded(art['title'], exclude_words, phonetic, whole_word):
                continue
            seen_links.add(art['link'])
            seen_titles.add(title_key)
            ts = art['published'].strftime('%Y-%m-%d %H:%M:%S')
            # GDELT trae su propio channel_id por artículo (nacional/
            # internacional, ver alerts/gdelt.py) -- Google Noticias no lo
            # trae, así que cae al channel_id fijo de siempre.
            art_channel_id = art.get('channel_id', channel_id)
            # extra_data: solo GDELT vía BigQuery lo trae (personas,
            # organizaciones, temas, tono, citas -- ver
            # alerts/gdelt_bigquery.py); NULL para todo lo demás.
            extra = art.get('extra_data')
            extra_json = json.dumps(extra, ensure_ascii=False) if extra else None
            cur = adb.execute("""INSERT OR IGNORE INTO matches
                (search_id, keyword, channel_id, channel_name, timestamp, matched_text, source_url, channel_domain, extra_data)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (s['id'], kw, art_channel_id, art['source'], ts, art['title'], art['link'],
                 art.get('source_domain'), extra_json))
            total += cur.rowcount
    return total


def _poll_news_for_search(adb, s, keywords: list[str], exclude_words: list[str] | None = None,
                           date_from: str | None = None, date_to: str | None = None) -> int:
    """Google Noticias -- ver _poll_articles_for_search. El RSS tope en
    ~100 resultados por consulta (alerts/googlenews.py:GOOGLE_RSS_CAP), con
    fuerte sesgo hacia lo más reciente -- para el fetch histórico de un
    rango amplio (ej. un mes), pedir todo el rango de un jalón entierra
    casi todo lo de semanas atrás bajo los resultados de los últimos días.
    fetch_articles_range bisecta el rango recursivamente solo cuando hace
    falta, hasta llegar a un solo día -- ahí sí es el límite duro del feed
    (no entiende horas en after:/before:, no hay paginación oficial)."""
    return _poll_articles_for_search(adb, s, keywords, exclude_words, date_from, date_to,
                                      gnews_fetch, fetch_articles_range, NEWS_CHANNEL_ID)


def _poll_gdelt_for_search(adb, s, keywords: list[str], exclude_words: list[str] | None = None,
                            date_from: str | None = None, date_to: str | None = None) -> int:
    """GDELT (cobertura internacional, opt-in -- ver alerts/channel_types.py
    MEDIA_TYPES). Corre AMBAS fuentes cuando hay credenciales de BigQuery
    (alerts/gdelt_bigquery.py) guardadas en /settings -- son complementarias,
    no intercambiables:
    - BigQuery/GKG: sin límite de tasa, metadata rica (extra_data), pero
      solo encuentra el término si el GKG lo extrajo como entidad FORMAL
      (persona/organización) -- confirmado con datos reales: "CJNG" y
      "el mencho" (apodo/siglas, nunca se extraen como entidad formal)
      dan 0 en BigQuery pero el DOC API sí los encuentra (250, tope real,
      para "CJNG") porque busca texto completo del artículo, no entidades.
    - DOC API (alerts/gdelt.py): búsqueda de texto completo real, pero con
      tope de 250/consulta y rate limit propio (1 consulta/5s) -- tolerable
      porque el poll ya corre cada GDELT_POLL_MINUTES, no en cada ciclo.
    Sin credenciales de BigQuery, corre solo el DOC API (como siempre)."""
    total = 0
    row = adb.execute("SELECT value FROM settings WHERE key='bigquery_credentials_json'").fetchone()
    if row and row['value']:
        from alerts.gdelt_bigquery import fetch_articles as bq_fetch, fetch_articles_range as bq_fetch_range
        # BigQuery cobra por partición de día escaneada, no por lo nuevo que
        # haya de verdad -- sin esto, cada poll (cada 30 min) volvía a leer
        # "los últimos 2 días" completos una y otra vez, la gran mayoría ya
        # visto. Se acota al DELTA real: desde el día del último fetch
        # exitoso de esta búsqueda hasta hoy (normalmente el mismo día =
        # 1 sola partición, no 2). Solo aplica al poll en vivo (sin fechas
        # explícitas); el histórico ya manda su propio rango.
        bq_date_from, bq_date_to = date_from, date_to
        if not bq_date_from and not bq_date_to:
            last_fetch = s['gdelt_last_fetch'] if 'gdelt_last_fetch' in s.keys() else None
            today = datetime.now().strftime('%Y-%m-%d')
            bq_date_from = last_fetch[:10] if last_fetch else today
            bq_date_to = today
        total += _poll_articles_for_search(adb, s, keywords, exclude_words, bq_date_from, bq_date_to,
                                            bq_fetch, bq_fetch_range, GDELT_CHANNEL_ID)
    total += _poll_articles_for_search(adb, s, keywords, exclude_words, date_from, date_to,
                                        gdelt_fetch, gdelt_fetch_range, GDELT_CHANNEL_ID)
    return total


# ── YouTube ────────────────────────────────────────────────────────────────
YOUTUBE_SEARCH_COST_UNITS = 100  # search.list, ver alerts/youtube.py docstring


def _track_youtube_quota(adb, units: int = YOUTUBE_SEARCH_COST_UNITS) -> None:
    """Contador propio de cuota consumida -- la YouTube Data API v3 no
    expone la cuota restante en la respuesta (a diferencia de otras APIs de
    Google), solo se ve en la consola de Google Cloud. Se guarda en
    settings (mismo patrón que otros contadores del proyecto) para poder
    mostrar un estimado en /settings sin tener que ir a revisar la consola.
    Se reinicia solo cuando cambia la fecha (cuota diaria)."""
    today = date.today().isoformat()
    stored_date = _get_setting(adb, 'youtube_quota_date', '')
    used = int(_get_setting(adb, 'youtube_quota_used', '0')) if stored_date == today else 0
    _set_setting(adb, 'youtube_quota_date', today)
    _set_setting(adb, 'youtube_quota_used', str(used + units))
    adb.commit()


def _poll_youtube_for_search(adb, s, keywords: list[str], exclude_words: list[str] | None = None,
                              date_from: str | None = None, date_to: str | None = None) -> int:
    """Busca videos nuevos en YouTube por cada keyword (YouTube Data API v3),
    obtiene la transcripción de cada video NUEVO -- primero intenta los
    captions nativos de YouTube (rápido, sin CPU); si el video no tiene en
    español, cae a descargar el audio y transcribirlo localmente (CPU, ver
    alerts/youtube.py) -- y guarda como matches los segmentos de 30s donde
    aparece alguna keyword de la búsqueda (channel_id=YOUTUBE_CHANNEL_ID,
    channel_name=título del video, source_url=link con &t=<segundo> al
    momento exacto). Si el video dura <= YOUTUBE_FULL_TRANSCRIPT_MAX_SEC
    (ver alerts/youtube.py), también guarda la transcripción COMPLETA en
    youtube_transcripts, no solo el fragmento con la palabra clave. tabla
    youtube_processed evita reprocesar un video ya visto, aunque esa vez no
    haya dado match -- la transcripción es cara y no cambia entre ciclos."""
    from alerts.youtube import search_videos, get_transcript, detect_language, FULL_TRANSCRIPT_MAX_SEC, _api_key
    api_key = _api_key(adb)
    if not api_key:
        return 0
    total = 0
    tmp_root = BASE_DIR / 'tmp_youtube'
    for kw in keywords:
        try:
            results = search_videos(kw, date_from, date_to, api_key)
            _track_youtube_quota(adb)
        except Exception as e:
            logger.error(f"[YouTube] búsqueda '{kw}' error de API: {e}")
            continue
        for vid in results:
            seen = adb.execute("SELECT 1 FROM youtube_processed WHERE video_id=?",
                                (vid['video_id'],)).fetchone()
            if seen:
                continue
            try:
                result = get_transcript(vid['video_id'], tmp_root, logger)
            except Exception as e:
                logger.error(f"[YouTube] transcripción de {vid['video_id']} falló: {e}")
                result = None
            if result is None:
                adb.execute("INSERT OR IGNORE INTO youtube_processed (video_id) VALUES (?)",
                            (vid['video_id'],))
                adb.commit()
                continue
            segments = result['segments']

            # Filtro de idioma -- a pedido explícito, solo interesan
            # español e inglés. La metadata de YouTube (duración/título) no
            # siempre delata el idioma hablado, así que se detecta sobre una
            # muestra de la transcripción ya obtenida (primeros ~5 min,
            # suficiente para una detección confiable sin gastar en el resto
            # de videos largos). Si no se pudo determinar, se conserva --
            # mejor un falso positivo ocasional que perder contenido real
            # por una detección ambigua.
            sample = ' '.join(t for _, t in segments[:10]).strip()
            lang = detect_language(sample) if sample else None
            if lang is not None and lang not in {'es', 'en'}:
                adb.execute("INSERT OR IGNORE INTO youtube_processed (video_id) VALUES (?)",
                            (vid['video_id'],))
                adb.commit()
                continue

            if result['duration'] is not None and result['duration'] <= FULL_TRANSCRIPT_MAX_SEC:
                full_text = ' '.join(t for _, t in result['raw_segments'])
                adb.execute("""INSERT OR REPLACE INTO youtube_transcripts
                    (video_id, title, channel, published, url, source, duration_sec, full_text, segments)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (vid['video_id'], vid['title'], vid.get('channel'), vid['published'], vid['url'],
                     result['source'], result['duration'], full_text, json.dumps(result['raw_segments'])))

            try:
                # La API de YouTube entrega publishedAt en UTC -- .astimezone()
                # sin argumento lo convierte a la hora local del sistema
                # (America/Mexico_City), igual que datetime.now() en el resto
                # del código. Sin esto, cada match de YouTube quedaba con el
                # timestamp adelantado por el offset UTC completo (6h aquí).
                published = datetime.fromisoformat(vid['published'].replace('Z', '+00:00')).astimezone()
            except ValueError:
                published = datetime.now()
            dedup_on = bool(s['dedup_channel']) if 'dedup_channel' in s.keys() else True

            # Primero se recorre TODO el video contando, por palabra clave,
            # cuántos segmentos de 30s la mencionan y en cuál apareció
            # primero -- así, si luego se colapsa a una sola fila por video
            # (dedup_on), esa fila puede mostrar "detectada N veces" en vez
            # de perder esa información (antes se dejaba de revisar el
            # resto del video en cuanto aparecía la primera mención).
            kw_occurrences: dict[str, int] = {}
            first_seg: dict[str, tuple[float, str]] = {}
            for offset, text in segments:
                if _excluded(text, exclude_words, bool(s['phonetic']), bool(s['whole_word'])):
                    continue
                for kw2 in keywords:
                    if _match(text, kw2, bool(s['phonetic']), bool(s['whole_word'])):
                        kw_occurrences[kw2] = kw_occurrences.get(kw2, 0) + 1
                        first_seg.setdefault(kw2, (offset, text))

            if dedup_on and first_seg:
                # Un video es contenido fijo, no un canal en vivo -- a
                # diferencia de TV/radio (donde el mismo tema puede
                # legítimamente repetirse horas después), aquí varias
                # menciones dentro del mismo video son la MISMA mención
                # repetida, no varias distintas. Una sola fila para todo el
                # video: la palabra que apareció primero cronológicamente.
                kw2 = min(first_seg, key=lambda k: first_seg[k][0])
                to_insert = [(kw2, *first_seg[kw2], kw_occurrences[kw2])]
            else:
                to_insert = [(kw2, *first_seg[kw2], kw_occurrences[kw2]) for kw2 in first_seg]

            for kw2, offset, text, cnt in to_insert:
                # offset como segundos agregados -- cada segmento del mismo
                # video necesita un timestamp distinto para no chocar
                # contra el índice único (search_id, keyword, channel_id,
                # timestamp) y perder coincidencias reales.
                seg_ts = (published + timedelta(seconds=offset)).strftime('%Y-%m-%d %H:%M:%S')
                cur = adb.execute("""INSERT OR IGNORE INTO matches
                    (search_id, keyword, channel_id, channel_name, timestamp, matched_text, source_url, occurrence_count)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (s['id'], kw2, YOUTUBE_CHANNEL_ID, vid['title'], seg_ts, text,
                     f"{vid['url']}&t={int(offset)}s", cnt))
                total += cur.rowcount

            adb.execute("INSERT OR IGNORE INTO youtube_processed (video_id) VALUES (?)",
                        (vid['video_id'],))
            adb.commit()
    return total


# ── Histórico de búsquedas nuevas, en paralelo por canal ─────────────────────
def _backfill_channel(args):
    """Corre en un proceso worker aparte (ver ProcessPoolExecutor en
    _process). Procesa TODO el rango de fechas de UN solo canal, en orden
    cronológico (ORDER BY id ASC, que en transcriptions.db equivale a orden
    de timestamp dentro de un mismo canal) -- necesario para que
    _recent_match_exists seguya viendo, al momento de cada fila, todas las
    coincidencias de ESE canal ya insertadas antes en el tiempo. No hace
    falta coordinarse con los demás workers: dedup es por (search_id,
    keyword, channel_id), así que un canal nunca depende de lo que otro
    canal esté insertando.

    Abre sus propias conexiones a las DB -- sqlite3 no se puede compartir
    entre procesos (a diferencia de entre hilos)."""
    (search_id, channel_id, keywords, exclude_words, phonetic, whole_word,
     dedup_on, exclude_music, date_start, date_end) = args

    tdb = _tdb()
    adb = _adb()
    BATCH = 2000
    last_id  = 0
    n_done   = 0
    try:
        while True:
            hist = tdb.execute("""
                SELECT id, channel_id, channel_name, timestamp, text, has_music
                FROM transcriptions
                WHERE id > ? AND channel_id = ?
                  AND timestamp >= ? AND timestamp <= ?
                ORDER BY id ASC
                LIMIT ?
            """, (last_id, channel_id, date_start + ' 00:00:00', date_end + ' 23:59:59', BATCH)).fetchall()
            if not hist:
                break
            for row in hist:
                text = row['text'] or ''
                if text and text != '[~]' and not (exclude_music and 'has_music' in row.keys() and row['has_music']) \
                        and not _excluded(text, exclude_words, phonetic, whole_word):
                    matching_kws = [kw for kw in keywords if _match(text, kw, phonetic, whole_word)]
                    if dedup_on:
                        matching_kws = matching_kws[:1]
                    for kw in matching_kws:
                        if dedup_on and _recent_match_exists(adb, search_id, kw, channel_id, row['timestamp']):
                            continue
                        ctx = _with_context_chunks(tdb, channel_id, row['timestamp'], text)
                        adb.execute("""INSERT OR IGNORE INTO matches
                            (search_id, keyword, channel_id, channel_name, timestamp, matched_text, has_music)
                            VALUES (?,?,?,?,?,?,?)""",
                            (search_id, kw, channel_id, row['channel_name'], row['timestamp'], ctx,
                             int(row['has_music']) if 'has_music' in row.keys() else 0))
            last_id = hist[-1]['id']
            n_done += len(hist)
            adb.execute("UPDATE searches SET init_rows_done = init_rows_done + ? WHERE id=?",
                        (len(hist), search_id))
            adb.commit()
            if len(hist) < BATCH:
                break
    finally:
        tdb.close()
        adb.close()
    return channel_id, n_done


# ── Ciclo principal ───────────────────────────────────────────────────────────
def _process(adb, tdb, smtp, cfg=None):
    today = date.today().isoformat()
    now   = datetime.now()

    # 1. Inicializar búsquedas nuevas (histórico desde date_start) -- NO se
    # filtra por status='active': initialized=0 ya es una señal explícita
    # (búsqueda recién creada, o editada con needs_reinit en alerts/app.py)
    # y nunca se pone así "por accidente". Filtrar por status dejaba
    # atascada para siempre cualquier búsqueda editada cuyo date_end ya
    # hubiera pasado -- _close_expired() la marca 'completed' en el mismo
    # ciclo, y con el filtro de abajo el histórico (ya borrado por la
    # edición) nunca se volvía a poblar.
    new_searches = adb.execute(
        "SELECT * FROM searches WHERE initialized=0"
    ).fetchall()
    if new_searches:
        # Un solo ProcessPoolExecutor COMPARTIDO entre TODAS las búsquedas
        # nuevas de este ciclo, no uno por búsqueda -- si varias se crearon
        # casi al mismo tiempo, sus tareas (una por canal) quedan mezcladas
        # en la misma cola y el pool las reparte según van terminando, así
        # que los núcleos disponibles se reparten solos entre las búsquedas
        # activas en vez de procesarlas una por una de principio a fin. Y si
        # solo hay una búsqueda nueva, esa es la única con tareas en la
        # cola, así que de facto se queda con el pool completo -- no hace
        # falta ninguna lógica especial para "toda la potencia si está sola".
        meta = {}       # search_id -> datos para cerrarla cuando terminen sus canales
        all_tasks = []  # (search_id, channel_id, ...) para el pool
        for s in new_searches:
            keywords      = json.loads(s['keywords'])
            exclude_words = json.loads(s['exclude_words']) if 'exclude_words' in s.keys() and s['exclude_words'] else []
            phonetic    = bool(s['phonetic'])
            whole_word  = bool(s['whole_word'])
            media_types = parse_media_types(s['media_types'] if 'media_types' in s.keys() else None)
            dedup_on    = bool(s['dedup_channel']) if 'dedup_channel' in s.keys() else True
            exclude_music = bool(s['exclude_music']) if 'exclude_music' in s.keys() and s['exclude_music'] is not None else True
            # Restricción opcional a un subconjunto de canales de TV/radio
            # (ver searches.channels, alerts/app.py:_tv_radio_channels) --
            # lista vacía = sin restricción, todos los canales del media_type
            # elegido (comportamiento de siempre).
            allowed_channels = json.loads(s['channels']) if 'channels' in s.keys() and s['channels'] else []

            # Un proceso worker por canal (ver _backfill_channel arriba) --
            # channel_type() es una función pura de channel_id, así que
            # filtrar por media_types aquí (canal completo) reemplaza el
            # filtro que antes se hacía fila por fila, y de paso evita leer
            # canales irrelevantes.
            channel_rows = tdb.execute("""
                SELECT DISTINCT channel_id FROM transcriptions
                WHERE timestamp >= ? AND timestamp <= ?
            """, (s['date_start'] + ' 00:00:00', s['date_end'] + ' 23:59:59')).fetchall()
            channel_ids = [r['channel_id'] for r in channel_rows if channel_type(r['channel_id']) in media_types]
            if allowed_channels:
                channel_ids = [c for c in channel_ids if c in allowed_channels]

            # BUG corregido (2026-09-11): antes este total contaba TODAS las
            # transcripciones del rango sin filtrar por media_types -- una
            # búsqueda de solo GDELT (sin tv/radio) mostraba "Procesando
            # histórico... 0/274,458 registros" en la barra de progreso
            # aunque no hubiera ni un canal real que escanear (channel_ids
            # vacío), confundiendo con trabajo de TV/radio que nunca se iba
            # a hacer. Ahora solo cuenta lo que realmente se va a procesar.
            if channel_ids:
                placeholders = ','.join('?' * len(channel_ids))
                total_count = tdb.execute(f"""
                    SELECT COUNT(*) FROM transcriptions
                    WHERE timestamp >= ? AND timestamp <= ? AND channel_id IN ({placeholders})
                """, [s['date_start'] + ' 00:00:00', s['date_end'] + ' 23:59:59'] + channel_ids).fetchone()[0]
            else:
                total_count = 0
            adb.execute("UPDATE searches SET init_rows_total=?, init_rows_done=0 WHERE id=?",
                        (total_count, s['id']))
            adb.commit()

            meta[s['id']] = {'s': s, 'keywords': keywords, 'exclude_words': exclude_words,
                              'media_types': media_types, 'pending': len(channel_ids), 'total_hist': 0}
            for ch in channel_ids:
                all_tasks.append((s['id'], ch, keywords, exclude_words, phonetic, whole_word,
                                   dedup_on, exclude_music, s['date_start'], s['date_end']))

        def _finish(sid):
            m = meta[sid]
            s = m['s']
            if 'news' in m['media_types']:
                n_news = _poll_news_for_search(adb, s, m['keywords'], m['exclude_words'],
                                                date_from=s['date_start'], date_to=s['date_end'])
                adb.execute("UPDATE searches SET news_last_fetch=? WHERE id=?",
                            (now.strftime('%Y-%m-%d %H:%M:%S'), sid))
                adb.commit()
                logger.info(f"Búsqueda {sid} '{s['name']}': {n_news} artículos de Google Noticias "
                            f"(histórico {s['date_start']}..{s['date_end']}).")
            if 'gdelt' in m['media_types']:
                n_gdelt = _poll_gdelt_for_search(adb, s, m['keywords'], m['exclude_words'],
                                                  date_from=s['date_start'], date_to=s['date_end'])
                adb.execute("UPDATE searches SET gdelt_last_fetch=? WHERE id=?",
                            (now.strftime('%Y-%m-%d %H:%M:%S'), sid))
                adb.commit()
                logger.info(f"Búsqueda {sid} '{s['name']}': {n_gdelt} artículos de GDELT "
                            f"(histórico {s['date_start']}..{s['date_end']}).")
            adb.execute("UPDATE searches SET initialized=1 WHERE id=?", (sid,))
            adb.commit()
            logger.info(f"Búsqueda {sid} '{s['name']}' inicializada: {m['total_hist']} registros históricos revisados.")

        # Búsquedas nuevas sin ningún canal en su rango (nada que paralelizar,
        # p.ej. un rango de fechas sin ninguna transcripción todavía) -- se
        # cierran de una vez, nunca van a entrar a all_tasks/pending.
        for sid, m in meta.items():
            if m['pending'] == 0:
                _finish(sid)

        if all_tasks:
            n_workers = min(len(all_tasks), MAX_BACKFILL_WORKERS)
            with ProcessPoolExecutor(max_workers=n_workers) as pool:
                futures = {pool.submit(_backfill_channel, t): t[0] for t in all_tasks}
                logger.info(f"Histórico: {len(all_tasks)} tareas canal/búsqueda de {len(meta)} búsqueda(s) "
                            f"nueva(s), {n_workers} procesos en paralelo.")
                for fut in as_completed(futures):
                    sid = futures[fut]
                    try:
                        _ch, n_done = fut.result()
                        meta[sid]['total_hist'] += n_done
                    except Exception:
                        logger.exception(f"Búsqueda {sid} '{meta[sid]['s']['name']}': fallo procesando histórico de un canal")
                    meta[sid]['pending'] -= 1
                    if meta[sid]['pending'] == 0:
                        _finish(sid)

    # 1.5 Re-consultar Google Noticias para búsquedas activas ya inicializadas
    # (las nuevas ya se cubrieron arriba, en su fetch histórico inicial)
    news_due = adb.execute(f"""
        SELECT * FROM searches
        WHERE status='active' AND initialized=1
        AND date_start <= ? AND date_end >= ?
        AND media_types LIKE '%news%'
        AND (news_last_fetch IS NULL OR news_last_fetch <= datetime('now','localtime','-{NEWS_POLL_MINUTES} minutes'))
    """, (today, today)).fetchall()
    for s in news_due:
        keywords = json.loads(s['keywords'])
        exclude_words = json.loads(s['exclude_words']) if 'exclude_words' in s.keys() and s['exclude_words'] else []
        n_news = _poll_news_for_search(adb, s, keywords, exclude_words)
        adb.execute("UPDATE searches SET news_last_fetch=? WHERE id=?", (now.strftime('%Y-%m-%d %H:%M:%S'), s['id']))
        adb.commit()
        if n_news:
            logger.info(f"Búsqueda {s['id']} '{s['name']}': {n_news} artículos nuevos de Google Noticias.")

    # 1.6 Re-consultar GDELT para búsquedas activas ya inicializadas -- misma
    # idea que 1.5, fuente opt-in aparte (ver alerts/channel_types.py).
    gdelt_due = adb.execute(f"""
        SELECT * FROM searches
        WHERE status='active' AND initialized=1
        AND date_start <= ? AND date_end >= ?
        AND media_types LIKE '%gdelt%'
        AND (gdelt_last_fetch IS NULL OR gdelt_last_fetch <= datetime('now','localtime','-{GDELT_POLL_MINUTES} minutes'))
    """, (today, today)).fetchall()
    for s in gdelt_due:
        keywords = json.loads(s['keywords'])
        exclude_words = json.loads(s['exclude_words']) if 'exclude_words' in s.keys() and s['exclude_words'] else []
        n_gdelt = _poll_gdelt_for_search(adb, s, keywords, exclude_words)
        adb.execute("UPDATE searches SET gdelt_last_fetch=? WHERE id=?", (now.strftime('%Y-%m-%d %H:%M:%S'), s['id']))
        adb.commit()
        if n_gdelt:
            logger.info(f"Búsqueda {s['id']} '{s['name']}': {n_gdelt} artículos nuevos de GDELT.")

    # 2. Procesar nuevas transcripciones (delta desde último ID)
    last_id = int(_get_setting(adb, 'watcher_last_id', '0'))
    rows = tdb.execute("""
        SELECT id, channel_id, channel_name, timestamp, text, has_music
        FROM transcriptions WHERE id > ?
        ORDER BY id ASC LIMIT 500
    """, (last_id,)).fetchall()

    if not rows:
        return

    active = adb.execute("""
        SELECT s.*, u.tg_chat_id as u_tg_chat_id
        FROM searches s JOIN users u ON s.user_id = u.id
        WHERE s.status='active' AND s.initialized=1
        AND s.date_start <= ? AND s.date_end >= ?
    """, (today, today)).fetchall()

    immediate_email   = []   # correos inmediatos (solo modo 'immediate')
    immediate_telegram = []  # alertas Telegram (independiente del modo de correo)

    tg_token       = (cfg or {}).get('tg_token', '')
    tg_chat_global = (cfg or {}).get('tg_chat_id', '')

    if active:
        for row in rows:
            text = row['text'] or ''
            if not text or text == '[~]':
                continue
            row_type = channel_type(row['channel_id'])
            for s in active:
                media_types = parse_media_types(s['media_types'] if 'media_types' in s.keys() else None)
                if row_type not in media_types:
                    continue
                allowed_channels = json.loads(s['channels']) if 'channels' in s.keys() and s['channels'] else []
                if allowed_channels and row['channel_id'] not in allowed_channels:
                    continue
                keywords      = json.loads(s['keywords'])
                exclude_words = json.loads(s['exclude_words']) if 'exclude_words' in s.keys() and s['exclude_words'] else []
                phonetic   = bool(s['phonetic'])
                whole_word = bool(s['whole_word'])
                dedup_on   = bool(s['dedup_channel']) if 'dedup_channel' in s.keys() else True
                exclude_music = bool(s['exclude_music']) if 'exclude_music' in s.keys() and s['exclude_music'] is not None else True
                if exclude_music and 'has_music' in row.keys() and row['has_music']:
                    continue
                if _excluded(text, exclude_words, phonetic, whole_word):
                    continue
                # Ver mismo comentario en el bloque de histórico más arriba --
                # con dedup_on, varias keywords que matchean el mismo
                # fragmento cuentan como una sola coincidencia.
                matching_kws = [kw for kw in keywords if _match(text, kw, phonetic, whole_word)]
                if dedup_on:
                    matching_kws = matching_kws[:1]
                for kw in matching_kws:
                    if dedup_on and _recent_match_exists(adb, s['id'], kw, row['channel_id'], row['timestamp']):
                        continue
                    # El chunk siguiente casi nunca existe todavía en este
                    # punto (se procesa ~al momento) -- se agrega si ya
                    # llegó, si no cae al texto del chunk solo, sin error.
                    ctx = _with_context_chunks(tdb, row['channel_id'], row['timestamp'], text)
                    adb.execute("""INSERT OR IGNORE INTO matches
                        (search_id, keyword, channel_id, channel_name, timestamp, matched_text, has_music)
                        VALUES (?,?,?,?,?,?,?)""",
                        (s['id'], kw, row['channel_id'], row['channel_name'],
                         row['timestamp'], ctx, int(row['has_music']) if 'has_music' in row.keys() else 0))

                    if 'threshold_alert_enabled' in s.keys() and s['threshold_alert_enabled']:
                        _check_threshold_alert(adb, s, cfg, smtp)

                    base = {
                        'search_id':   s['id'],
                        'search_name': s['name'],
                        'keyword':     kw,
                        'channel_id':  row['channel_id'],
                        'channel_name':row['channel_name'],
                        'timestamp':   row['timestamp'],
                        'matched_text':ctx,
                    }

                    # Email: solo en modo inmediato con correo configurado
                    if s['delivery_mode'] == 'immediate' and s['report_email']:
                        immediate_email.append({**base, 'report_email': s['report_email']})

                    # Telegram: siempre que esté activado, sin importar modo de correo
                    notify_tg = s['notify_telegram'] if 'notify_telegram' in s.keys() else 0
                    if notify_tg:
                        if not tg_token:
                            logger.warning(f"[TG] Búsqueda {s['id']}: notify_telegram=1 pero sin tg_token en ajustes.")
                        else:
                            chat_id = (s['u_tg_chat_id'] if 'u_tg_chat_id' in s.keys() else '') or tg_chat_global
                            if not chat_id:
                                logger.warning(f"[TG] Búsqueda {s['id']}: sin chat_id (ni en perfil de usuario ni en ajustes globales).")
                            else:
                                immediate_telegram.append({**base, 'chat_id': chat_id})
        adb.commit()

    # 3. Correos inmediatos
    if smtp and immediate_email:
        for m in immediate_email:
            try:
                send_immediate(m, smtp)
            except Exception as e:
                logger.error(f"Email inmediato error: {e}")

    # 4. Alertas Telegram (independientes del modo de entrega de correo)
    for m in immediate_telegram:
        try:
            ok = tg_notify_match(
                tg_token, m['chat_id'],
                m['search_name'], m['keyword'],
                m['channel_name'], m['timestamp'],
                m['matched_text'],
            )
            if ok:
                logger.info(f"[TG] Alerta enviada: búsqueda '{m['search_name']}' · kw '{m['keyword']}'")
            else:
                logger.error(f"[TG] Fallo al enviar alerta: búsqueda '{m['search_name']}' · chat_id '{m['chat_id']}'")
        except Exception as e:
            logger.error(f"[TG] Excepción: {e}")


    _set_setting(adb, 'watcher_last_id', str(rows[-1]['id']))
    adb.commit()


def _daily_reports(adb, smtp, cfg=None):
    """Corre en _daily_jobs_loop (hilo aparte, NO el loop rápido de 5s) --
    incluye una llamada al LLM local por búsqueda (ver rag.summarize_matches,
    medido ~85s en producción para 40 coincidencias), demasiado lenta para
    el loop de detección en vivo.

    Antes exigía report_email, así que una búsqueda 'daily' configurada
    SOLO con Telegram nunca recibía su reporte diario -- se corrige aquí
    agregando el JOIN a users para tg_chat_id (mismo patrón que ya usa
    _process para el resto de las alertas de Telegram) y enviando por
    cualquiera de los dos canales que esté configurado, no solo correo."""
    if datetime.now().hour < 7:
        return
    today = date.today().isoformat()
    searches = adb.execute("""
        SELECT s.*, u.tg_chat_id as u_tg_chat_id
        FROM searches s JOIN users u ON s.user_id = u.id
        WHERE s.delivery_mode='daily' AND s.status='active'
        AND (s.last_daily_report IS NULL OR s.last_daily_report < ?)
    """, (today,)).fetchall()
    if not searches:
        return
    tg_token       = (cfg or {}).get('tg_token', '')
    tg_chat_global = (cfg or {}).get('tg_chat_id', '')
    for s in searches:
        matches = adb.execute("""
            SELECT * FROM matches WHERE search_id=?
            AND date(found_at,'localtime') = date('now','localtime')
            ORDER BY found_at
        """, (s['id'],)).fetchall()
        matches_list = [dict(m) for m in matches]
        summary = ''
        if matches_list:
            try:
                from rag import summarize_matches
                summary = summarize_matches(s['name'], matches_list)
            except Exception as e:
                logger.error(f"Resumen LLM error search {s['id']}: {e}")
        try:
            if smtp and s['report_email']:
                send_daily_report(s, matches_list, smtp, summary=summary)
            if s['notify_telegram'] and tg_token and matches_list:
                chat_id = (s['u_tg_chat_id'] if 'u_tg_chat_id' in s.keys() else '') or tg_chat_global
                if chat_id and summary:
                    msg = (f"<b>Resumen diario — {html.escape(s['name'])}</b>\n"
                           f"{len(matches_list)} coincidencias hoy\n\n{html.escape(summary)}")
                    tg_send_telegram(tg_token, chat_id, msg)
            adb.execute("UPDATE searches SET last_daily_report=? WHERE id=?", (today, s['id']))
            adb.commit()
        except Exception as e:
            logger.error(f"Reporte diario error search {s['id']}: {e}")


def _weekly_excel_reports(adb, smtp):
    """Reporte Excel semanal opt-in (weekly_excel_report=1) -- corre en
    _daily_jobs_loop, NO en el loop rápido de 5s: armar un Workbook puede
    tardar según el volumen de coincidencias de la semana, y no debe
    retrasar la detección en vivo de las demás búsquedas activas."""
    if not smtp:
        return
    if datetime.now().hour < 7 or datetime.now().weekday() != WEEKLY_REPORT_WEEKDAY:
        return
    today = date.today().isoformat()
    searches = adb.execute("""
        SELECT * FROM searches
        WHERE weekly_excel_report=1 AND report_email IS NOT NULL AND report_email!=''
        AND status='active'
        AND (last_weekly_report IS NULL OR last_weekly_report < ?)
    """, (today,)).fetchall()
    if not searches:
        return
    from alerts.excel_report import build_workbook
    from alerts.mailer      import send_weekly_excel_report
    tdb = _tdb()
    week_start = (date.today() - timedelta(days=7)).isoformat()
    week_end   = (date.today() - timedelta(days=1)).isoformat()
    for s in searches:
        matches = adb.execute("""
            SELECT * FROM matches WHERE search_id=?
            AND date(timestamp) BETWEEN ? AND ?
            ORDER BY timestamp ASC
        """, (s['id'], week_start, week_end)).fetchall()
        try:
            output = build_workbook([(s, matches)], adb, tdb)
            safe_name = re.sub(r'[^A-Za-z0-9_-]', '_', s['name'])
            fname = f"reporte_semanal_{safe_name}_{week_start}_a_{week_end}.xlsx"
            send_weekly_excel_report(dict(s), len(matches), output.getvalue(), fname, smtp)
            adb.execute("UPDATE searches SET last_weekly_report=? WHERE id=?", (today, s['id']))
            adb.commit()
        except Exception as e:
            logger.error(f"Reporte semanal Excel error search {s['id']}: {e}")
    tdb.close()


def _close_expired(adb):
    """Marca como 'completed' cualquier búsqueda cuyo date_end ya pasó, sin importar el modo."""
    today = date.today().isoformat()
    rows = adb.execute("""
        SELECT id, name FROM searches
        WHERE status='active' AND date_end < ?
    """, (today,)).fetchall()
    for s in rows:
        adb.execute("UPDATE searches SET status='completed' WHERE id=?", (s['id'],))
        logger.info(f"Búsqueda {s['id']} '{s['name']}' marcada como completada (date_end expirado).")
    if rows:
        adb.commit()


def _final_reports(adb, smtp):
    if not smtp:
        return
    today = date.today().isoformat()
    searches = adb.execute("""
        SELECT * FROM searches
        WHERE delivery_mode='final' AND report_email IS NOT NULL AND report_email!=''
        AND status IN ('active','completed') AND date_end < ?
        AND (last_daily_report IS NULL OR last_daily_report < date_end)
    """, (today,)).fetchall()
    for s in searches:
        matches = adb.execute(
            "SELECT * FROM matches WHERE search_id=? ORDER BY found_at", (s['id'],)
        ).fetchall()
        try:
            send_final_report(s, [dict(m) for m in matches], smtp)
            adb.execute("UPDATE searches SET status='completed', last_daily_report=? WHERE id=?",
                        (today, s['id']))
            adb.commit()
        except Exception as e:
            logger.error(f"Reporte final error search {s['id']}: {e}")


def _loop():
    logger.info("Watcher iniciado.")
    while True:
        try:
            adb  = _adb()
            tdb  = _tdb()
            cfg  = _full_cfg(adb)
            smtp = cfg if (cfg.get('smtp_host') or cfg.get('gmail_refresh_token')) else None
            epg_schema(adb)
            epg_refresh(adb)
            _close_expired(adb)
            _process(adb, tdb, smtp, cfg)
            _final_reports(adb, smtp)
            adb.close()
            tdb.close()
        except Exception as e:
            logger.exception(f"Error en watcher: {e}")
        time.sleep(POLL_INTERVAL)


def _youtube_loop():
    """Hilo separado del loop rápido de 5s (_loop) a propósito: descargar y
    transcribir un video de YouTube por CPU puede tardar bastantes segundos
    o minutos, y _loop() procesa TODAS las búsquedas activas de TV/radio
    cada 5s -- si YouTube corriera ahí adentro, un solo video lento
    congelaría la detección en vivo de todo lo demás. Revisa cada minuto
    qué búsquedas ya están "due": la primera vez (youtube_last_fetch NULL,
    apenas se creó/activó la búsqueda) o una vez al día después de
    YOUTUBE_DAILY_HOUR si el último fetch fue un día anterior -- cuota de la
    API es el límite, no la frescura de resultados."""
    logger.info(f"YouTube loop iniciado (1 consulta al crear + 1 diaria a las {YOUTUBE_DAILY_HOUR}:00).")
    while True:
        try:
            adb = _adb()
            today = date.today().isoformat()
            due = adb.execute(f"""
                SELECT * FROM searches
                WHERE status='active'
                AND date_start <= ? AND date_end >= ?
                AND media_types LIKE '%youtube%'
                AND (
                    youtube_last_fetch IS NULL
                    OR (
                        CAST(strftime('%H', 'now', 'localtime') AS INTEGER) >= {YOUTUBE_DAILY_HOUR}
                        AND date(youtube_last_fetch) < date('now', 'localtime')
                    )
                )
            """, (today, today)).fetchall()
            for s in due:
                keywords = json.loads(s['keywords'])
                exclude_words = json.loads(s['exclude_words']) if 'exclude_words' in s.keys() and s['exclude_words'] else []
                try:
                    n = _poll_youtube_for_search(adb, s, keywords, exclude_words,
                                                  date_from=s['date_start'], date_to=s['date_end'])
                    if n:
                        logger.info(f"[YouTube] Búsqueda {s['id']} '{s['name']}': {n} coincidencias nuevas.")
                except Exception as e:
                    logger.exception(f"[YouTube] Error procesando búsqueda {s['id']}: {e}")
                adb.execute("UPDATE searches SET youtube_last_fetch=? WHERE id=?",
                            (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), s['id']))
                adb.commit()
            adb.close()
        except Exception as e:
            logger.exception(f"Error en youtube_loop: {e}")
        time.sleep(60)


def _daily_jobs_loop():
    """Hilo separado del loop rápido de 5s (_loop), mismo motivo que
    _youtube_loop: el reporte diario (que incluye el resumen ejecutivo por
    IA, ver rag.py:summarize_matches) y el reporte Excel semanal pueden
    tardar del orden de decenas de segundos por búsqueda -- si corrieran
    dentro de _loop(), varias búsquedas 'daily'/semanales activas sumarían
    minutos de bloqueo cada mañana, retrasando la detección en vivo de
    coincidencias para TODAS las búsquedas activas mientras tanto. Sondeo
    cada 60s (no 5s): ambas tareas ya se autolimitan por hora del día
    (hour < 7) y, en el caso semanal, también por día de la semana."""
    logger.info("Hilo de reportes diarios/semanales iniciado.")
    while True:
        try:
            adb  = _adb()
            cfg  = _full_cfg(adb)
            smtp = cfg if (cfg.get('smtp_host') or cfg.get('gmail_refresh_token')) else None
            _daily_reports(adb, smtp, cfg)
            _weekly_excel_reports(adb, smtp)
            adb.close()
        except Exception as e:
            logger.exception(f"Error en daily_jobs_loop: {e}")
        time.sleep(60)


def _world_pulse_loop():
    """Hilo aparte -- recalcula el caché de /pulso (alerts/world_pulse.py)
    antes de que expire, para ambos scopes (mundo/méxico). Si no hay
    credenciales de BigQuery configuradas, get_pulse() devuelve
    'available': False de inmediato, sin costo -- este loop puede correr
    siempre sin necesidad de chequear configuración aparte.

    El intervalo (REFRESH_MIN) vive en alerts/world_pulse.py, no aquí --
    junto a CACHE_TTL_MIN, con quien tiene que mantenerse sincronizado."""
    from alerts.world_pulse import get_pulse, REFRESH_MIN
    logger.info(f"Hilo de refresco de Pulso del mundo iniciado (cada {REFRESH_MIN} min).")
    while True:
        try:
            for scope in ('world', 'mx'):
                get_pulse(scope, force=True)
        except Exception as e:
            logger.exception(f"Error refrescando Pulso del mundo: {e}")
        time.sleep(REFRESH_MIN * 60)


def start_watcher():
    t = threading.Thread(target=_loop, daemon=True, name='alertas-watcher')
    t.start()
    ty = threading.Thread(target=_youtube_loop, daemon=True, name='alertas-youtube')
    ty.start()
    td = threading.Thread(target=_daily_jobs_loop, daemon=True, name='alertas-daily-jobs')
    td.start()
    tp = threading.Thread(target=_world_pulse_loop, daemon=True, name='alertas-world-pulse')
    tp.start()
    return t
