"""
alerts/app.py — Dashboard de Alertas de Transcripción TV.
Flask app: auth, búsquedas, coincidencias, reportes, panel admin.
"""
import json
import os
import re
import sqlite3
import unicodedata
from datetime    import date, datetime, timedelta
from functools   import wraps, lru_cache
from pathlib     import Path

from flask              import (Flask, g, flash, jsonify, redirect,
                                render_template, request, session, url_for,
                                send_file)
from markupsafe         import Markup, escape as html_escape
from werkzeug.security  import check_password_hash, generate_password_hash

BASE_DIR  = Path(__file__).parent.parent
ALERTS_DB = BASE_DIR / 'alerts.db'
TRANS_DB  = BASE_DIR / 'transcriptions.db'


# ── Resaltado de keywords ─────────────────────────────────────────────────────
@lru_cache(maxsize=8192)
def _strip_acc(s: str) -> str:
    s = unicodedata.normalize('NFD', s.lower())
    return ''.join(c for c in s if unicodedata.category(c) != 'Mn')

@lru_cache(maxsize=8192)
def _phonetic(s: str) -> str:
    t = _strip_acc(s)
    t = re.sub(r'\bh', '', t)
    t = t.replace('v', 'b').replace('ll', 'y').replace('z', 's').replace('ck', 'k')
    t = re.sub(r'qu([ei])', r'k\1', t)
    t = re.sub(r'c([ei])', r's\1', t)
    t = re.sub(r'g([ei])', r'j\1', t)
    t = re.sub(r'x', 'ks', t)
    return t

CHUNK_SECONDS  = 30   # debe coincidir con worker.py / transcriber.py
# Deben coincidir con CHUNK_SEP_PREV / CHUNK_SEP_NEXT en alerts/watcher.py.
CHUNK_SEP_PREV = '⁠'   # word joiner (U+2060) -- antes del chunk original
CHUNK_SEP_NEXT = '​'   # zero-width space (U+200B) -- después del chunk original

def _locate_keyword(text, keyword, phonetic=False, whole_word=False):
    """Encuentra el índice (basado en palabras) de la primera ocurrencia
    de la keyword (o frase de varias palabras). Devuelve (idx_word, total_words)
    o (None, total_words). Usa la misma lógica de match que _highlight
    (acento-insensitive y opcionalmente fonético).

    En modo whole_word, un token solo cuenta si la keyword aparece delimitada
    por separadores de palabra dentro de él (ej. "día" no debe casar con
    "diálogo") — se comprueba con límites \\w sobre el token normalizado, no
    con una lista fija de signos de puntuación, para cubrir cualquier
    puntuación pegada (paréntesis, comillas, etc.)."""
    if not text or not keyword:
        return None, 0
    words = text.split()
    n = len(words)
    if n == 0:
        return None, 0
    norm     = _phonetic if phonetic else _strip_acc
    kw_words = [norm(w) for w in keyword.split()]
    k = len(kw_words)
    if k == 0:
        return None, n

    if k == 1:
        kw_n    = kw_words[0]
        pattern = re.compile(r'(?<!\w)' + re.escape(kw_n) + r'(?!\w)') if whole_word else None
        for i, w in enumerate(words):
            w_n = norm(w)
            if whole_word:
                if pattern.search(w_n):
                    return i, n
            elif kw_n in w_n:
                return i, n
        return None, n

    # Frase de varias palabras: buscar la secuencia consecutiva de tokens.
    for i in range(n - k + 1):
        window = [norm(words[i + j]) for j in range(k)]
        if whole_word:
            if window == kw_words:
                return i, n
        elif all(kw_words[j] in window[j] for j in range(k)):
            return i, n
    return None, n

def _center_text(text, idx_word, words_each_side=50):
    """Recorta el texto centrado en la palabra match: N palabras antes y N después.
    Si no hay match, devuelve las primeras 2N+1 palabras."""
    if not text:
        return ''
    words = text.split()
    n = len(words)
    if n == 0:
        return ''
    if idx_word is None:
        idx_word = 0
    start = max(0, idx_word - words_each_side)
    end   = min(n, idx_word + words_each_side + 1)
    snippet = ' '.join(words[start:end])
    if start > 0:
        snippet = '… ' + snippet
    if end < n:
        snippet = snippet + ' …'
    return snippet

def _precise_timestamp(start_ts, idx_word, total_words, chunk_sec=CHUNK_SECONDS):
    """Estima el timestamp absoluto del momento exacto en que se mencionó la
    palabra dentro del chunk. start_ts es el inicio del audio (ya garantizado
    en worker.py al momento de captura, no de inferencia). Si la palabra está
    en la posición k de N, asumimos que se mencionó en (k+0.5)/N del chunk.
    Precisión típica ±2-3s para chunks de 30s."""
    if not start_ts or idx_word is None or not total_words:
        return start_ts
    try:
        # Soporta ISO con o sin milisegundos
        dt = datetime.fromisoformat(start_ts)
        offset = ((idx_word + 0.5) / total_words) * chunk_sec
        return (dt + timedelta(seconds=offset)).isoformat(sep=' ', timespec='seconds')
    except Exception:
        return start_ts

def _current_chunk_span(text: str) -> str:
    """El texto del chunk ORIGINAL (30s) donde está garantizado el match,
    despegando el chunk previo y/o siguiente que matched_text pueda traer
    pegados para dar más contexto (ver CHUNK_SEP_PREV/NEXT y
    alerts/watcher.py)."""
    t = text
    if CHUNK_SEP_PREV in t:
        t = t.split(CHUNK_SEP_PREV, 1)[1]
    if CHUNK_SEP_NEXT in t:
        t = t.split(CHUNK_SEP_NEXT, 1)[0]
    return t.strip()

def _enrich_match(m, phonetic=False, whole_word=False):
    """Convierte una Row a dict y agrega: precise_timestamp, centered_text y
    channel_kind ('tv'/'radio'/'youtube' — para no mostrar el ícono de video
    en coincidencias de radio, que nunca tienen clip). `m` puede ser
    sqlite3.Row o dict."""
    from alerts.channel_types import channel_type
    md = dict(m)
    text = md.get('matched_text') or ''
    kw   = md.get('keyword') or ''
    md['channel_kind'] = channel_type(md.get('channel_id'))

    # precise_timestamp asume que el índice/total de palabras corresponden a
    # UN chunk de CHUNK_SECONDS (30s) de audio -- válido para TV/radio/
    # YouTube (todos transcritos en ventanas de 30s reales), pero NO para
    # Google Noticias: ahí matched_text es el título del artículo, no un
    # fragmento cronometrado, y aplicarle esta fórmula desplazaba el
    # timestamp hasta ~30s de más según en qué palabra del título cayera la
    # keyword -- eso descuadraba el orden cronológico frente a TV/radio al
    # combinarse en la misma tabla. Para noticias se usa el timestamp real
    # (la hora de publicación) tal cual, sin ajuste.
    if md['channel_kind'] == 'news':
        idx_chunk = None
        md['precise_timestamp'] = md.get('timestamp')
    else:
        # Como matched_text puede traer pegados el chunk previo y/o el
        # siguiente para dar más contexto, se busca la keyword SOLO dentro
        # del chunk original (nunca en los pegados -- podría aparecer por
        # casualidad ahí y hacer que se centre en la ocurrencia equivocada)
        # y se usa ese conteo de palabras, no el del texto completo -- si
        # no, la fracción se diluye con las palabras pegadas y el timestamp
        # estimado queda mal, sobre todo cerca del inicio/final del chunk
        # original, justo los casos que esto arregla.
        current_chunk = _current_chunk_span(text)
        idx_chunk, total_chunk = _locate_keyword(current_chunk, kw, phonetic=phonetic, whole_word=whole_word)
        md['precise_timestamp'] = _precise_timestamp(md.get('timestamp'), idx_chunk, total_chunk)

    # Posición de esa misma ocurrencia dentro del texto COMPLETO (con
    # contexto pegado), para centrar el snippet mostrado -- se calcula
    # sumando cuántas "palabras" (vía text.split(), separadores incluidos)
    # hay antes del chunk actual, en vez de volver a buscar la keyword en el
    # texto completo (que podría encontrar antes una ocurrencia casual en el
    # chunk previo pegado).
    prefix_words = 0
    if CHUNK_SEP_PREV in text:
        prefix_words = len(text.split(CHUNK_SEP_PREV, 1)[0].split()) + 1
    idx_full = (idx_chunk + prefix_words) if idx_chunk is not None else None
    raw_centered = _center_text(text, idx_full, words_each_side=50)
    cleaned = raw_centered.replace(CHUNK_SEP_PREV, ' ').replace(CHUNK_SEP_NEXT, ' ')
    md['centered_text'] = re.sub(r' {2,}', ' ', cleaned).strip()
    if md['channel_kind'] == 'youtube' and md.get('source_url'):
        # Miniatura pública de YouTube -- imagen estática de su propio CDN,
        # sin descargar/procesar nada de nuestro lado (a diferencia del
        # snapshot de TV, que sí recorta un frame real por ffmpeg).
        m_vid = re.search(r'[?&]v=([\w-]{11})', md['source_url'])
        md['youtube_video_id'] = m_vid.group(1) if m_vid else None
    if md['channel_kind'] == 'radio':
        from alerts.channel_logos import get_logo
        md['channel_logo'] = get_logo(md.get('channel_name'))
    return md

def _highlight(text, keyword, phonetic=False, whole_word=False):
    """Resalta todas las ocurrencias de keyword en text con <mark>.
    Usa comparación sin acentos/mayúsculas; en modo fonético detecta
    palabras fonéticamente equivalentes aunque se escriban diferente.
    En modo whole_word exige límites de palabra (\\w) alrededor de la
    keyword, para no resaltar "día" dentro de "diálogo"."""
    if not text or not keyword:
        return Markup(html_escape(text or ''))
    text, keyword = str(text), str(keyword)
    if phonetic:
        # kw_words: una entrada por palabra de la keyword (puede ser una frase
        # de varias palabras, ej. "Nuevo Pantene Molecular Bond Repair") --
        # antes esto comparaba la fonética de la FRASE COMPLETA contra la de
        # cada palabra suelta del texto, lo cual nunca podía coincidir salvo
        # que la keyword fuera de una sola palabra. Ahora se desliza una
        # ventana de k palabras consecutivas, igual que _locate_keyword.
        kw_words = [_phonetic(w) for w in keyword.split()]
        k = len(kw_words)
        parts = re.split(r'(\s+)', text)
        word_pos = [i for i, p in enumerate(parts) if p.strip()]
        marked = [False] * len(word_pos)
        for start in range(len(word_pos) - k + 1):
            ok = True
            for j in range(k):
                w_ph = _phonetic(parts[word_pos[start + j]])
                if whole_word:
                    if not re.search(r'(?<!\w)' + re.escape(kw_words[j]) + r'(?!\w)', w_ph):
                        ok = False
                        break
                elif kw_words[j] not in w_ph:
                    ok = False
                    break
            if ok:
                for j in range(k):
                    marked[start + j] = True
        out = []
        wi = 0
        for p in parts:
            if not p.strip():
                out.append(str(html_escape(p)))
                continue
            if marked[wi]:
                out.append(f'<mark>{html_escape(p)}</mark>')
            else:
                out.append(str(html_escape(p)))
            wi += 1
        return Markup(''.join(out))
    # Búsqueda exacta sin acentos/mayúsculas: regex case-insensitive sobre texto escapeado
    esc_text = str(html_escape(text))
    esc_kw   = re.escape(str(html_escape(keyword)))
    pattern  = (r'(?<!\w)' if whole_word else '') + esc_kw + (r'(?!\w)' if whole_word else '')
    result   = re.sub(pattern, lambda m: f'<mark>{m.group()}</mark>',
                      esc_text, flags=re.IGNORECASE)
    return Markup(result)


def _mark_fragment(text, fragment):
    """Resalta el fragmento común de un cluster de similitud (alerts/similarity.py)
    dentro de text. El fragmento se extrajo comparando SOLO dos miembros del
    cluster (el más largo contra su mejor match -- ver _common_fragment), así
    que puede no ser substring literal de un tercer miembro que tenga alguna
    palabra distinta en medio del mismo guion. Por eso no se busca substring
    exacto: se resalta el tramo coincidente MÁS LARGO entre el fragmento y
    este texto en particular (tolerante a mayúsculas/acentos/puntuación),
    tal cual está escrito en esta ocurrencia. Con coincidencias muy cortas
    (probablemente casuales) no se resalta nada."""
    if not text or not fragment:
        return Markup(html_escape(text or ''))
    from alerts.similarity import _normalize_with_map
    norm_text, tmap = _normalize_with_map(text)
    norm_frag, _    = _normalize_with_map(fragment)
    if not norm_frag:
        return Markup(html_escape(text))
    # Camino rápido: la mayoría de las apariciones SÍ contienen el fragmento
    # literal (normalizado) -- un substring simple es prácticamente gratis.
    # SequenceMatcher (O(n·m) en el peor caso) solo se usa como respaldo para
    # el minoría de casos con alguna palabra distinta en medio, y ahora que
    # matched_text puede llegar a ~1000 caracteres (contexto duplicado) es
    # demasiado costoso para llamarlo en cada aparición sin necesidad.
    idx = norm_text.find(norm_frag)
    if idx != -1:
        start = tmap[idx]
        end   = tmap[idx + len(norm_frag) - 1] + 1
    else:
        # SequenceMatcher sobre el texto COMPLETO es caro (ahora hasta ~1000
        # caracteres, con el contexto duplicado) para un respaldo que además
        # casi nunca encuentra nada útil (medido: ~80% de las veces que el
        # fragmento completo no aparece literal, tampoco hay ningún tramo
        # >=20 caracteres que resaltar). Antes de pagar ese costo se prueban
        # 3 sondeos baratos (substring de ~25 caracteres al inicio/medio/
        # final del fragmento) -- si ninguno aparece ni aproximadamente, se
        # deja el texto sin resaltar en vez de gastar la comparación completa
        # para, la mayoría de las veces, no encontrar nada de todos modos.
        PROBE_LEN, WINDOW = 25, 200
        nf = len(norm_frag)
        probe_positions = {0, max(0, nf // 2 - PROBE_LEN // 2), max(0, nf - PROBE_LEN)}
        pidx = -1
        for p in probe_positions:
            probe = norm_frag[p:p + PROBE_LEN]
            if len(probe) < 8:
                continue
            found = norm_text.find(probe)
            if found != -1:
                pidx = found - p  # posición estimada del inicio del fragmento
                break
        if pidx == -1:
            return Markup(html_escape(text))
        from difflib import SequenceMatcher
        wstart = max(0, pidx - WINDOW)
        wend   = min(len(norm_text), pidx + nf + WINDOW)
        window = norm_text[wstart:wend]
        sm = SequenceMatcher(None, norm_frag, window, autojunk=False)
        m  = sm.find_longest_match(0, nf, 0, len(window))
        if m.size < min(20, nf // 2):
            return Markup(html_escape(text))
        start = tmap[wstart + m.b]
        end   = tmap[wstart + m.b + m.size - 1] + 1
    # Concatenar objetos Markup con "+" re-escapa el lado que no es Markup (para
    # evitar XSS por descuido) -- por eso cada pieza se pasa por str() primero
    # y el envoltorio Markup() se aplica solo una vez, al final, sobre texto
    # plano ya escapado (mismo patrón que usa _highlight arriba).
    return Markup(
        str(html_escape(text[:start])) +
        f'<mark>{html_escape(text[start:end])}</mark>' +
        str(html_escape(text[end:]))
    )


# ── DB schema ─────────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    email         TEXT    UNIQUE NOT NULL,
    password_hash TEXT    NOT NULL,
    role          TEXT    DEFAULT 'user',
    active        INTEGER DEFAULT 1,
    created_at    TEXT    DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS searches (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL,
    name              TEXT    NOT NULL,
    keywords          TEXT    NOT NULL,
    phonetic          INTEGER DEFAULT 0,
    whole_word        INTEGER DEFAULT 0,
    date_start        TEXT    NOT NULL,
    date_end          TEXT    NOT NULL,
    status            TEXT    DEFAULT 'active',
    delivery_mode     TEXT    DEFAULT 'final',
    report_email      TEXT,
    last_daily_report TEXT,
    initialized       INTEGER DEFAULT 0,
    created_at        TEXT    DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS matches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id    INTEGER NOT NULL,
    keyword      TEXT    NOT NULL,
    channel_id   INTEGER,
    channel_name TEXT,
    timestamp    TEXT,
    matched_text TEXT,
    emailed      INTEGER DEFAULT 0,
    found_at     TEXT    DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (search_id) REFERENCES searches(id)
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- Videos de YouTube ya descargados/transcritos (alerts/watcher.py
-- _poll_youtube_for_search) -- evita re-descargar y re-transcribir por CPU
-- un video ya visto en un ciclo anterior, tenga o no match esa vez.
CREATE TABLE IF NOT EXISTS youtube_processed (
    video_id     TEXT PRIMARY KEY,
    processed_at TEXT DEFAULT (datetime('now','localtime'))
);
-- Transcripción COMPLETA de un video (no solo el fragmento con la palabra
-- clave) para videos de hasta YOUTUBE_FULL_TRANSCRIPT_MAX_SEC (ver
-- alerts/youtube.py) -- separada de `transcriptions` (TV/radio) porque ahí
-- un "canal" es un flujo continuo con su propia línea de tiempo; cada
-- video de YouTube es una pieza aislada, mezclarlos bajo un solo
-- channel_id compartido no tendría sentido para navegar por fecha/hora.
CREATE TABLE IF NOT EXISTS youtube_transcripts (
    video_id     TEXT PRIMARY KEY,
    title        TEXT,
    channel      TEXT,
    published    TEXT,
    url          TEXT,
    source       TEXT,   -- 'captions' (nativo de YouTube) o 'asr' (transcrito localmente)
    duration_sec INTEGER,
    full_text    TEXT,
    segments     TEXT,   -- JSON [[segundo_inicio, texto], ...]
    fetched_at   TEXT DEFAULT (datetime('now','localtime'))
);
-- Diccionario de corrección post-transcripción (ver text_corrections.py)
-- -- errores CONSISTENTES y conocidos (nombres propios, siglas, nombres de
-- estación) que el motor repite siempre igual. pattern se busca como
-- palabra completa, sin distinguir mayúsculas/acentos en la búsqueda
-- (aunque sí en el reemplazo, para poder fijar la capitalización correcta).
CREATE TABLE IF NOT EXISTS text_corrections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern     TEXT NOT NULL,
    replacement TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_m_search ON matches(search_id);
CREATE INDEX IF NOT EXISTS idx_m_found  ON matches(found_at);
CREATE INDEX IF NOT EXISTS idx_s_user   ON searches(user_id);
CREATE INDEX IF NOT EXISTS idx_s_status ON searches(status, date_start, date_end);
"""


def _init_db():
    conn = sqlite3.connect(str(ALERTS_DB))
    conn.executescript(_SCHEMA)
    # Clave secreta persistente
    if not conn.execute("SELECT 1 FROM settings WHERE key='secret_key'").fetchone():
        conn.execute("INSERT INTO settings (key,value) VALUES ('secret_key',?)",
                     (os.urandom(32).hex(),))
    # Schema migrations
    for col, dfn in [
        ('notify_telegram',  'INTEGER DEFAULT 0'),   # searches table
        ('init_rows_done',   'INTEGER DEFAULT 0'),
        ('init_rows_total',  'INTEGER DEFAULT 0'),
        ('whole_word',       'INTEGER DEFAULT 0'),
        # 'tv', 'radio', o 'tv,radio' -- qué tipos de canal cubre la búsqueda.
        # Default incluye todo lo existente al momento de la migración, para
        # no cambiar el comportamiento de búsquedas ya creadas.
        ('media_types',      "TEXT DEFAULT 'tv,radio'"),
        # Última vez que se consultó Google Noticias para esta búsqueda (ver
        # alerts/googlenews.py) -- NULL hasta el primer fetch.
        ('news_last_fetch',  'TEXT'),
        # Igual que news_last_fetch pero para YouTube (alerts/youtube.py) --
        # intervalo propio y más largo (YOUTUBE_POLL_MINUTES en watcher.py)
        # porque cada ciclo puede implicar descargar/transcribir video nuevo.
        ('youtube_last_fetch', 'TEXT'),
        # Si la misma palabra se repite en el mismo canal dentro de 1 minuto
        # (misma nota/segmento), contar solo una coincidencia -- ver
        # alerts/watcher.py _recent_match_exists(). Default 1 (activado) para
        # que las búsquedas ya creadas también queden con el comportamiento
        # esperado sin tener que editarlas.
        ('dedup_channel',    'INTEGER DEFAULT 1'),
        # Palabras/frases que anulan una coincidencia si aparecen en el MISMO
        # fragmento de texto -- ej. buscar "rocha" mientras se excluye
        # "reprochar"/"derrochar" (que la contienen como substring, ver
        # alerts/watcher.py _excluded()). JSON, igual formato que keywords.
        ('exclude_words',    "TEXT DEFAULT '[]'"),
        # Omitir fragmentos de TV/radio marcados con música (ver
        # music_classifier.py, transcriptions.has_music) al buscar -- un
        # comercial/canción rara vez es relevante para una búsqueda de texto
        # normal. Default 1 (excluir) tanto para búsquedas nuevas como ya
        # existentes: es seguro retroactivamente porque el historial previo
        # a esta función nunca se clasificó (has_music=0 siempre), así que
        # no oculta nada que ya existiera.
        ('exclude_music',    'INTEGER DEFAULT 1'),
        # Alerta de pico de menciones -- ORTOGONAL a delivery_mode (no un
        # nuevo valor del enum): una búsqueda puede querer su reporte
        # diario/final normal Y ADEMÁS un aviso urgente si de repente se
        # dispara la frecuencia. Mismo criterio que notify_telegram, que
        # tampoco depende de delivery_mode. Ver watcher.py:_check_threshold_alert.
        ('threshold_alert_enabled', 'INTEGER DEFAULT 0'),
        ('threshold_count',         'INTEGER DEFAULT 5'),
        ('threshold_window_min',    'INTEGER DEFAULT 30'),
        # Cooldown: no volver a alertar hasta que pase threshold_window_min
        # desde la última alerta de esta búsqueda.
        ('last_threshold_alert',    'TEXT'),
        # Reporte Excel semanal por correo, opt-in -- ver
        # watcher.py:_weekly_excel_reports.
        ('weekly_excel_report', 'INTEGER DEFAULT 0'),
        ('last_weekly_report',  'TEXT'),
    ]:
        try:
            conn.execute(f"ALTER TABLE searches ADD COLUMN {col} {dfn}")
            conn.commit()
        except Exception:
            pass
    try:
        conn.execute("ALTER TABLE users ADD COLUMN tg_chat_id TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass
    try:
        # URL del artículo original -- solo se llena para matches de Google
        # Noticias (channel_id=NEWS_CHANNEL_ID); NULL para TV/radio.
        conn.execute("ALTER TABLE matches ADD COLUMN source_url TEXT")
        conn.commit()
    except Exception:
        pass
    try:
        # Dominio del medio (ej. "www.infobae.com") -- solo Google Noticias,
        # viene del atributo url="..." de <source> en el feed RSS (ver
        # alerts/googlenews.py). Se usa para mostrar el favicon del medio en
        # los resultados, gratis (servicio público de favicons, sin scrapear
        # cada artículo por su imagen real).
        conn.execute("ALTER TABLE matches ADD COLUMN channel_domain TEXT")
        conn.commit()
    except Exception:
        pass
    try:
        # Copia de transcriptions.has_music al momento del match -- solo
        # TV/radio (watcher.py la llena ahí). Se guarda aquí en vez de
        # buscarla en vivo por join porque matches ya desnormaliza
        # channel_name/timestamp/etc. con el mismo criterio, y así no hace
        # falta ir a transcriptions.db (otra base) para mostrar el ícono.
        conn.execute("ALTER TABLE matches ADD COLUMN has_music INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass
    try:
        # Cuántas veces se detectó esa palabra en TODO el contenido que
        # representa esta fila -- por ahora solo lo llena YouTube (watcher.py):
        # un video se colapsa a una sola coincidencia (ver el fix de
        # duplicados por video), y esto conserva cuántas veces en realidad
        # apareció la palabra en el video completo, no solo en el fragmento
        # mostrado. Default 1 -- una fila siempre representa al menos una
        # detección real.
        conn.execute("ALTER TABLE matches ADD COLUMN occurrence_count INTEGER DEFAULT 1")
        conn.commit()
    except Exception:
        pass
    # Eliminar duplicados antes de crear el índice único
    conn.execute("""
        DELETE FROM matches WHERE id NOT IN (
            SELECT MIN(id) FROM matches
            GROUP BY search_id, keyword, channel_id, timestamp
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_m_unique
        ON matches(search_id, keyword, channel_id, timestamp)
    """)
    conn.commit()
    conn.commit()
    conn.close()


def _get_secret_key() -> str:
    conn = sqlite3.connect(str(ALERTS_DB))
    row  = conn.execute("SELECT value FROM settings WHERE key='secret_key'").fetchone()
    conn.close()
    return row[0] if row else os.urandom(32).hex()


# cache_size/mmap_size más grandes que el default de SQLite (2MB) -- afinado
# para el dashboard sirviendo hasta ~20 máquinas consultando a la vez detrás
# de Gunicorn. Solo se aplica en conexiones de LECTURA (dashboard/watcher);
# los procesos de grabación/transcripción (transcriber_parakeet.py, etc.) no
# se tocan para no arriesgar la captura 24x7 en vivo.
_TUNE_PRAGMAS = (
    "PRAGMA synchronous=NORMAL",
    "PRAGMA cache_size=-64000",
    "PRAGMA mmap_size=268435456",
)


def _connect_trans_db(timeout: float = 10) -> sqlite3.Connection:
    conn = sqlite3.connect(str(TRANS_DB), timeout=timeout)
    conn.execute("PRAGMA journal_mode=WAL")
    for p in _TUNE_PRAGMAS:
        conn.execute(p)
    conn.row_factory = sqlite3.Row
    return conn


# ── App factory ───────────────────────────────────────────────────────────────
def create_app() -> Flask:
    _init_db()
    # Inicializar esquema EPG
    from alerts.epg import ensure_schema as _epg_schema
    _epg_conn = sqlite3.connect(str(ALERTS_DB))
    _epg_schema(_epg_conn)
    _epg_conn.close()

    app = Flask(__name__, template_folder='templates')
    app.secret_key = _get_secret_key()
    app.jinja_env.filters['fromjson']       = json.loads
    app.jinja_env.filters['highlight']      = _highlight
    app.jinja_env.filters['mark_fragment']  = _mark_fragment

    # ── DB helpers ─────────────────────────────────────────────────
    def db():
        if 'db' not in g:
            g.db = sqlite3.connect(str(ALERTS_DB), timeout=10)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA journal_mode=WAL")
            for p in _TUNE_PRAGMAS:
                g.db.execute(p)
        return g.db

    @app.teardown_appcontext
    def _close_db(_=None):
        c = g.pop('db', None)
        if c:
            c.close()

    @app.after_request
    def _no_cache_html(resp):
        """Evita caché agresivo en páginas HTML dinámicas. Los assets estáticos
        siguen cacheándose normalmente."""
        ct = resp.headers.get('Content-Type', '')
        if 'text/html' in ct:
            resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            resp.headers['Pragma']        = 'no-cache'
            resp.headers['Expires']       = '0'
        return resp

    # ── Auth decorators ────────────────────────────────────────────
    def login_required(f):
        @wraps(f)
        def inner(*a, **kw):
            if 'uid' not in session:
                return redirect(url_for('login'))
            return f(*a, **kw)
        return inner

    def admin_required(f):
        @wraps(f)
        def inner(*a, **kw):
            if 'uid' not in session:
                return redirect(url_for('login'))
            if session.get('role') != 'admin':
                flash('Acceso denegado.', 'danger')
                return redirect(url_for('dashboard'))
            return f(*a, **kw)
        return inner

    # ── Helpers de búsqueda ────────────────────────────────────────
    def _get_search(sid, require_owner=True):
        """Devuelve una búsqueda; admin ve todas, usuario solo las suyas."""
        if session.get('role') == 'admin':
            return db().execute(
                "SELECT s.*, u.name as u_name, u.email as u_email "
                "FROM searches s JOIN users u ON s.user_id=u.id WHERE s.id=?", (sid,)
            ).fetchone()
        if require_owner:
            return db().execute(
                "SELECT * FROM searches WHERE id=? AND user_id=?",
                (sid, session['uid'])
            ).fetchone()
        return db().execute("SELECT * FROM searches WHERE id=?", (sid,)).fetchone()

    # ══════════════════════════════════════════════════════════════
    # RUTAS AUTH
    # ══════════════════════════════════════════════════════════════
    @app.route('/')
    def index():
        return redirect(url_for('dashboard') if 'uid' in session else url_for('login'))

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            email = request.form.get('email', '').strip().lower()
            pw    = request.form.get('password', '')
            u     = db().execute(
                "SELECT * FROM users WHERE email=? AND active=1", (email,)
            ).fetchone()
            if u and check_password_hash(u['password_hash'], pw):
                session.clear()
                session.update(uid=u['id'], uname=u['name'], role=u['role'])
                return redirect(url_for('dashboard'))
            flash('Correo o contraseña incorrectos.', 'danger')
        return render_template('login.html')

    @app.route('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))

    @app.route('/profile', methods=['GET', 'POST'])
    @login_required
    def profile():
        d   = db()
        uid = session['uid']
        u   = d.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        cfg = {r['key']: r['value'] for r in d.execute("SELECT key,value FROM settings")}

        if request.method == 'POST':
            name       = request.form.get('name', '').strip()
            email      = request.form.get('email', '').strip().lower()
            pw         = request.form.get('password', '')
            pw2        = request.form.get('confirm', '')
            tg_chat_id = request.form.get('tg_chat_id', '').strip()

            if not name or not email:
                flash('Nombre y correo son obligatorios.', 'danger')
            elif d.execute("SELECT 1 FROM users WHERE email=? AND id!=?", (email, uid)).fetchone():
                flash('Ese correo ya está en uso.', 'danger')
            elif pw and pw != pw2:
                flash('Las contraseñas no coinciden.', 'danger')
            elif pw and len(pw) < 6:
                flash('Mínimo 6 caracteres en la contraseña.', 'danger')
            else:
                if pw:
                    d.execute("UPDATE users SET name=?,email=?,password_hash=?,tg_chat_id=? WHERE id=?",
                              (name, email, generate_password_hash(pw), tg_chat_id, uid))
                else:
                    d.execute("UPDATE users SET name=?,email=?,tg_chat_id=? WHERE id=?",
                              (name, email, tg_chat_id, uid))
                d.commit()
                session['uname'] = name
                flash('Perfil actualizado.', 'success')
                return redirect(url_for('profile'))

        return render_template('profile.html', u=u,
                               tg_token=cfg.get('tg_token', ''),
                               bot_name=cfg.get('tg_bot_name', ''))

    @app.route('/profile/test_telegram', methods=['POST'])
    @login_required
    def profile_test_telegram():
        from alerts.telegram import test_connection
        d       = db()
        uid     = session['uid']
        u       = d.execute("SELECT tg_chat_id FROM users WHERE id=?", (uid,)).fetchone()
        cfg     = {r['key']: r['value'] for r in d.execute("SELECT key,value FROM settings")}
        token   = cfg.get('tg_token', '')
        chat_id = u['tg_chat_id'] if u else ''
        if not token or not chat_id:
            return jsonify(ok=False, error='Bot no configurado o Chat ID vacío.')
        ok, err = test_connection(token, chat_id)
        return jsonify(ok=ok, error=err)

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            email= request.form.get('email', '').strip().lower()
            pw   = request.form.get('password', '')
            pw2  = request.form.get('confirm', '')
            if not all([name, email, pw]):
                flash('Todos los campos son obligatorios.', 'danger')
            elif pw != pw2:
                flash('Las contraseñas no coinciden.', 'danger')
            elif len(pw) < 6:
                flash('Mínimo 6 caracteres en la contraseña.', 'danger')
            elif db().execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                flash('Ese correo ya está registrado.', 'danger')
            else:
                n    = db().execute("SELECT COUNT(*) FROM users").fetchone()[0]
                role = 'admin' if n == 0 else 'user'
                db().execute(
                    "INSERT INTO users (name,email,password_hash,role) VALUES (?,?,?,?)",
                    (name, email, generate_password_hash(pw), role)
                )
                db().commit()
                flash('Cuenta creada. Inicia sesión.', 'success')
                return redirect(url_for('login'))
        return render_template('register.html')

    # ══════════════════════════════════════════════════════════════
    # DASHBOARD
    # ══════════════════════════════════════════════════════════════
    @app.route('/dashboard')
    @login_required
    def dashboard():
        d   = db()
        uid = session['uid']
        is_admin = session['role'] == 'admin'

        if is_admin:
            searches = d.execute("""
                SELECT s.*, u.name as u_name,
                       (SELECT COUNT(*) FROM matches m WHERE m.search_id=s.id) as mc
                FROM searches s JOIN users u ON s.user_id=u.id
                ORDER BY CASE s.status
                           WHEN 'active'    THEN 0
                           WHEN 'paused'    THEN 1
                           WHEN 'completed' THEN 2
                           ELSE 3 END,
                         s.created_at DESC
            """).fetchall()
            total_users = d.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            recent = d.execute("""
                SELECT m.*, s.name as s_name, s.id as s_id, u.name as u_name
                FROM matches m
                JOIN searches s ON m.search_id=s.id
                JOIN users    u ON s.user_id=u.id
                ORDER BY m.timestamp DESC LIMIT 25
            """).fetchall()
        else:
            searches = d.execute("""
                SELECT s.*,
                       (SELECT COUNT(*) FROM matches m WHERE m.search_id=s.id) as mc
                FROM searches s WHERE s.user_id=?
                ORDER BY CASE s.status
                           WHEN 'active'    THEN 0
                           WHEN 'paused'    THEN 1
                           WHEN 'completed' THEN 2
                           ELSE 3 END,
                         s.created_at DESC
            """, (uid,)).fetchall()
            total_users = None
            recent = d.execute("""
                SELECT m.*, s.name as s_name, s.id as s_id
                FROM matches m JOIN searches s ON m.search_id=s.id
                WHERE s.user_id=? ORDER BY m.timestamp DESC LIMIT 25
            """, (uid,)).fetchall()

        today_iso = date.today().isoformat()
        return render_template('dashboard.html',
            searches=searches,
            total_searches=len(searches),
            active_searches=sum(1 for s in searches
                                if s['status'] == 'active' and s['date_end'] >= today_iso),
            total_matches=sum(s['mc'] for s in searches),
            total_users=total_users,
            recent=recent,
        )

    # ══════════════════════════════════════════════════════════════
    # CONSULTAS IA (RAG)
    # ══════════════════════════════════════════════════════════════
    @app.route('/ask')
    @login_required
    def ask_page():
        from rag import RANGOS
        # Canales distintos visibles para el selector
        canales = []
        try:
            conn = _connect_trans_db(timeout=2)
            canales = [r[0] for r in conn.execute(
                "SELECT DISTINCT channel_name FROM transcriptions "
                "WHERE channel_name IS NOT NULL ORDER BY channel_name").fetchall()]
            conn.close()
        except Exception:
            pass
        return render_template('ask.html', rangos=RANGOS, canales=canales)

    @app.route('/api/ask', methods=['POST'])
    @login_required
    def api_ask():
        import json as _json
        from flask import Response, stream_with_context
        from rag   import ask_stream

        try:
            data = request.get_json(force=True) or {}
        except Exception:
            data = {}
        question = (data.get('q') or '').strip()
        rango    = data.get('rango', '24h')
        canal    = (data.get('canal') or '').strip() or None
        try:
            top_n = int(data.get('top_n') or 15)
        except Exception:
            top_n = 15

        if not question:
            return jsonify({'error': 'pregunta vacía'}), 400

        @stream_with_context
        def gen():
            for evt in ask_stream(question, rango=rango, canal=canal, top_n=top_n):
                yield _json.dumps(evt) + "\n"

        return Response(gen(), mimetype='application/x-ndjson')

    # ══════════════════════════════════════════════════════════════
    # BÚSQUEDAS — CRUD
    # ══════════════════════════════════════════════════════════════
    @app.route('/searches/new', methods=['GET', 'POST'])
    @login_required
    def search_new():
        if request.method == 'POST':
            name          = request.form.get('name', '').strip()
            kw_raw        = request.form.get('keywords', '').strip()
            excl_raw      = request.form.get('exclude_words', '').strip()
            phonetic      = 1 if request.form.get('phonetic') else 0
            whole_word    = 1 if request.form.get('whole_word') else 0
            dedup_channel = 1 if request.form.get('dedup_channel') else 0
            exclude_music = 1 if request.form.get('exclude_music') else 0
            d_start       = request.form.get('date_start', '')
            d_end         = request.form.get('date_end', '')
            dmode         = request.form.get('delivery_mode', 'final')
            remail        = request.form.get('report_email', '').strip()
            notify_tg     = 1 if request.form.get('notify_telegram') else 0
            media_types   = ','.join(request.form.getlist('media_types')) or 'tv,radio'
            threshold_enabled = 1 if request.form.get('threshold_alert_enabled') else 0
            threshold_count   = int(request.form.get('threshold_count') or 5)
            threshold_window  = int(request.form.get('threshold_window_min') or 30)
            weekly_excel      = 1 if request.form.get('weekly_excel_report') else 0

            if not all([name, kw_raw, d_start, d_end]):
                flash('Nombre, palabras y fechas son obligatorios.', 'danger')
            else:
                kws  = [k.strip() for k in re.split(r'[\n,]+', kw_raw) if k.strip()]
                excl = [e.strip() for e in re.split(r'[\n,]+', excl_raw) if e.strip()]
                cur = db().execute("""
                    INSERT INTO searches
                      (user_id,name,keywords,exclude_words,phonetic,whole_word,date_start,date_end,
                       delivery_mode,report_email,status,notify_telegram,media_types,dedup_channel,exclude_music,
                       threshold_alert_enabled,threshold_count,threshold_window_min,weekly_excel_report)
                    VALUES (?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?,?,?)
                """, (session['uid'], name, json.dumps(kws, ensure_ascii=False),
                      json.dumps(excl, ensure_ascii=False),
                      phonetic, whole_word, d_start, d_end, dmode, remail, notify_tg, media_types,
                      dedup_channel, exclude_music,
                      threshold_enabled, threshold_count, threshold_window, weekly_excel))
                db().commit()
                flash(f'Búsqueda «{name}» creada. Procesando el histórico…', 'success')
                # Al detalle, no al dashboard -- ahí ya está el banner de progreso
                # (spinner + % + ETA) que el watcher va llenando; en el dashboard
                # el usuario nunca lo veía porque el redirect lo mandaba de vuelta
                # a la lista sin ninguna señal de que algo se estaba procesando.
                return redirect(url_for('search_detail', sid=cur.lastrowid))
        from alerts.channel_types import MEDIA_TYPES, DEFAULT_MEDIA_TYPES
        # Fecha más antigua con transcripción real (TV/radio) -- para que el
        # calendario de "Fecha inicio" no deje elegir un día sin nada que
        # buscar. MIN(timestamp) sin envolver en date() sí aprovecha
        # idx_trans_timestamp (a diferencia de MIN(date(timestamp))), por
        # eso es instantáneo aunque la tabla tenga millones de filas.
        oldest = _connect_trans_db().execute("SELECT MIN(timestamp) FROM transcriptions").fetchone()[0]
        min_date = oldest[:10] if oldest else date.today().isoformat()

        # Rango de CLIPS reales de audio/video (distinto de min_date arriba,
        # que es solo el texto transcrito -- ese nunca se borra, pero los
        # archivos de audio/video sí se purgan del NAS por retención de
        # disco, así que puede haber coincidencias buscables sin clip
        # disponible para reproducir si la fecha es más vieja que esto).
        from alerts import library, audio_library
        video_range = library.overall_date_range()
        audio_range = audio_library.overall_date_range()
        starts = [r[0] for r in (video_range, audio_range) if r]
        ends   = [r[1] for r in (video_range, audio_range) if r]
        recording_range = (min(starts), max(ends)) if starts else None

        return render_template('search_new.html', today=date.today().isoformat(), min_date=min_date,
                               recording_range=recording_range,
                               media_types_choices=MEDIA_TYPES,
                               default_media_types=set(DEFAULT_MEDIA_TYPES.split(',')))

    def _match_where(sid, kws, chs, pfs, mts, date_from, date_to):
        """Construye WHERE + params para la tabla matches con todos los filtros activos.
        mts: subconjunto de {'tv','radio','news','youtube'} -- channel_kind no es una
        columna real (se deriva de channel_id, ver alerts/channel_types.py), así que se
        arma el mismo rango/valor por SQL en vez de re-consultar fila por fila."""
        conds, params = ['search_id=?'], [sid]
        if kws:
            conds.append(f"keyword IN ({','.join('?'*len(kws))})")
            params.extend(kws)
        if chs:
            conds.append(f"channel_name IN ({','.join('?'*len(chs))})")
            params.extend(chs)
        if mts:
            from alerts.channel_types import RADIO_CHANNEL_MIN, NEWS_CHANNEL_ID, YOUTUBE_CHANNEL_ID
            mt_conds = []
            if 'tv' in mts:
                mt_conds.append(f"(channel_id IS NULL OR channel_id < {RADIO_CHANNEL_MIN})")
            if 'radio' in mts:
                mt_conds.append(f"(channel_id >= {RADIO_CHANNEL_MIN} AND channel_id != {NEWS_CHANNEL_ID} AND channel_id != {YOUTUBE_CHANNEL_ID})")
            if 'news' in mts:
                mt_conds.append(f"channel_id = {NEWS_CHANNEL_ID}")
            if 'youtube' in mts:
                mt_conds.append(f"channel_id = {YOUTUBE_CHANNEL_ID}")
            if mt_conds:
                conds.append('(' + ' OR '.join(mt_conds) + ')')
        if date_from:
            conds.append("date(timestamp) >= ?"); params.append(date_from)
        if date_to:
            conds.append("date(timestamp) <= ?"); params.append(date_to)
        if pfs:
            conds.append(f"""EXISTS (
                SELECT 1 FROM epg_programmes e
                WHERE e.channel_name = matches.channel_name
                  AND e.start_ts <= matches.timestamp
                  AND e.stop_ts  >  matches.timestamp
                  AND e.title IN ({','.join('?'*len(pfs))})
            )""")
            params.extend(pfs)
        return ' AND '.join(conds), params

    @app.route('/searches/<int:sid>')
    @login_required
    def search_detail(sid):
        s = _get_search(sid)
        if not s:
            flash('Búsqueda no encontrada.', 'danger')
            return redirect(url_for('dashboard'))

        page      = request.args.get('page', 1, type=int)
        pp        = 50
        kfs       = request.args.getlist('kw')
        cfs       = request.args.getlist('ch')
        pfs       = request.args.getlist('prog')
        mfs       = request.args.getlist('mt')
        date_from = request.args.get('date_from', '')
        date_to   = request.args.get('date_to', '')

        where, params = _match_where(sid, kfs, cfs, pfs, mfs, date_from, date_to)

        d         = db()
        total     = d.execute(f"SELECT COUNT(*) FROM matches WHERE {where}", params).fetchone()[0]
        total_all = d.execute("SELECT COUNT(*) FROM matches WHERE search_id=?", (sid,)).fetchone()[0]
        matches_raw = d.execute(
            f"SELECT * FROM matches WHERE {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params + [pp, (page-1)*pp]
        ).fetchall()
        # Enriquece cada match con precise_timestamp y centered_text
        matches = [_enrich_match(m, phonetic=bool(s['phonetic']), whole_word=bool(s['whole_word']))
                   for m in matches_raw]
        # El ORDER BY de arriba usa el timestamp del chunk (inicio, redondeado
        # a 30s); precise_timestamp afina dentro de ese chunk según la
        # posición de la palabra. Como los chunks de canales distintos no
        # arrancan alineados al mismo segundo, dos matches de chunks
        # consecutivos pueden invertirse en el momento real que ocurrieron
        # -- se reordena esta página ya enriquecida por precise_timestamp
        # (el valor que de hecho se muestra) para que se vea cronológico.
        matches.sort(key=lambda m: m['precise_timestamp'] or '', reverse=True)
        kw_all = d.execute("SELECT DISTINCT keyword FROM matches WHERE search_id=? ORDER BY keyword", (sid,)).fetchall()
        ch_all = d.execute("SELECT DISTINCT channel_name FROM matches WHERE search_id=? ORDER BY channel_name", (sid,)).fetchall()
        # Solo los medios que esta búsqueda realmente cubre (s.media_types) --
        # mostrar "YouTube" como filtro en una búsqueda que nunca lo monitorea
        # daría un filtro que siempre vacía la tabla.
        from alerts.channel_types import MEDIA_TYPES, parse_media_types
        _search_mts = parse_media_types(s['media_types'] if 'media_types' in s.keys() else None)
        mt_all = [{'value': v, 'label': l} for v, l in MEDIA_TYPES if v in _search_mts]
        prog_all = d.execute("""
            SELECT DISTINCT e.title
            FROM matches m
            JOIN epg_programmes e
              ON e.channel_name = m.channel_name
             AND e.start_ts <= m.timestamp
             AND e.stop_ts  >  m.timestamp
            WHERE m.search_id = ?
            ORDER BY e.title
            LIMIT 200
        """, (sid,)).fetchall()
        # Mismo WHERE que la tabla de resultados -- antes estas dos usaban solo
        # search_id=?, así que "palabras" y "canales" seguían mostrando el
        # desglose de TODA la búsqueda aunque el usuario ya hubiera filtrado.
        kw_stats = d.execute(
            f"SELECT keyword, COUNT(*) cnt FROM matches WHERE {where} GROUP BY keyword ORDER BY cnt DESC", params
        ).fetchall()
        ch_stats = d.execute(
            f"SELECT channel_name, COUNT(*) cnt FROM matches WHERE {where} GROUP BY channel_name ORDER BY cnt DESC", params
        ).fetchall()

        # Heatmap: una fila por fecha real (date_start … hoy o date_end)
        hm_start = date.fromisoformat(s['date_start'])
        hm_end   = min(date.today(), date.fromisoformat(s['date_end']))
        if hm_end < hm_start:
            hm_end = hm_start
        hm_dates_list = []
        cur = hm_start
        while cur <= hm_end:
            hm_dates_list.append(cur.isoformat())
            cur += timedelta(days=1)
        n_days = len(hm_dates_list)
        date_idx = {dt: i for i, dt in enumerate(hm_dates_list)}

        # Mismo WHERE filtrado que arriba -- el mapa de calor mostraba
        # actividad de TODA la búsqueda incluso con filtros activos.
        hm_rows = d.execute(f"""
            SELECT date(timestamp) as day,
                   CAST(strftime('%H', timestamp) AS INTEGER) as hr,
                   COUNT(*) as cnt
            FROM matches WHERE {where}
            GROUP BY day, hr
        """, params).fetchall()
        heatmap = [[0] * 24 for _ in range(n_days)]
        for r in hm_rows:
            idx = date_idx.get(r['day'])
            if idx is not None:
                heatmap[idx][r['hr']] = r['cnt']
        hm_max = max((heatmap[di][h] for di in range(n_days) for h in range(24)), default=0)

        # EPG: obtener programa para cada coincidencia en esta página
        from alerts.epg import get_programme_at
        prog_map = {}
        for m in matches:
            if m['timestamp']:
                prog_map[m['id']] = get_programme_at(d, m['channel_name'] or '', m['timestamp'])

        # El auto-refresh en vivo (poll cada 8s) solo tiene sentido si a esta
        # búsqueda TODAVÍA le puede llegar algo nuevo -- si ya pasó su
        # date_end o está pausada/completada, no hay nada que esperar, y
        # además el mecanismo (after_id, sin respetar paginación) inyectaba
        # filas de otras páginas a la página 1 con solo con=8001 activo (ver
        # el bug real: id de inserción no corresponde al orden cronológico
        # en búsquedas históricas ya completas).
        live_eligible = (s['status'] == 'active' and s['date_end'] >= date.today().isoformat())

        return render_template('search_detail.html',
            s=s, keywords=json.loads(s['keywords']),
            matches=matches, total=total, page=page, pp=pp,
            pages=max(1, (total-1)//pp+1),
            kw_all=kw_all, ch_all=ch_all, prog_all=prog_all, mt_all=mt_all,
            kfs=kfs, cfs=cfs, pfs=pfs, mfs=mfs,
            date_from=date_from, date_to=date_to,
            total_all=total_all,
            kw_stats=kw_stats, ch_stats=ch_stats,
            heatmap=heatmap, hm_max=hm_max, hm_dates=hm_dates_list,
            prog_map=prog_map,
            live_eligible=live_eligible,
        )

    @app.route('/searches/<int:sid>/network')
    @login_required
    def search_network(sid):
        """Dashboard de comportamiento de medios: red de eco entre canales
        (quién origina un tema y quién lo repite después), flujo palabra
        clave -> medio -> canal (Sankey) y pulso temporal por tipo de medio.
        Respeta los mismos filtros activos en la vista de coincidencias."""
        s = _get_search(sid)
        if not s:
            flash('Búsqueda no encontrada.', 'danger')
            return redirect(url_for('dashboard'))

        kfs       = request.args.getlist('kw')
        cfs       = request.args.getlist('ch')
        pfs       = request.args.getlist('prog')
        mfs       = request.args.getlist('mt')
        date_from = request.args.get('date_from', '')
        date_to   = request.args.get('date_to', '')
        where, params = _match_where(sid, kfs, cfs, pfs, mfs, date_from, date_to)

        d = db()
        rows = d.execute(
            f"SELECT channel_id, channel_name, keyword, timestamp FROM matches WHERE {where}", params
        ).fetchall()

        from alerts.media_network import build_echo_network, build_sankey, build_stream_timeline
        network  = build_echo_network(rows)
        sankey   = build_sankey(rows)
        timeline = build_stream_timeline(rows)

        return render_template('search_network.html',
            s=s, total=len(rows),
            network=network, sankey=sankey, timeline=timeline,
            kfs=kfs, cfs=cfs, pfs=pfs, mfs=mfs, date_from=date_from, date_to=date_to,
            has_filters=bool(kfs or cfs or pfs or mfs or date_from or date_to),
        )

    @app.route('/searches/<int:sid>/similarities')
    @login_required
    def search_similarities(sid):
        s = _get_search(sid)
        if not s:
            flash('Búsqueda no encontrada.', 'danger')
            return redirect(url_for('dashboard'))
        from alerts.similarity import get_or_generate
        force = request.args.get('refresh') == '1'
        # Los pares sueltos (2 repeticiones) suelen ser ruido -- por defecto se
        # ocultan; con una búsqueda de miles de coincidencias puede haber
        # cientos de clusters, así que también se acota cuántos se dibujan.
        show_pairs = request.args.get('pairs') == '1'
        min_size   = 2 if show_pairs else 3
        RENDER_CAP        = 150
        UNIQUE_RENDER_CAP = 200

        report = get_or_generate(sid, force=force)
        all_clusters    = report['clusters']
        shown           = [c for c in all_clusters if c['size'] >= min_size][:RENDER_CAP]
        pairs_hidden    = sum(1 for c in all_clusters if c['size'] == 2) if not show_pairs else 0
        truncated       = max(0, len([c for c in all_clusters if c['size'] >= min_size]) - RENDER_CAP)
        unique_shown     = report['unique_matches'][:UNIQUE_RENDER_CAP]
        unique_truncated = max(0, len(report['unique_matches']) - UNIQUE_RENDER_CAP)

        # matched_text puede traer hasta 3 chunks pegados (~1500 caracteres,
        # ver alerts/watcher.py) para dar contexto/timestamp preciso -- eso
        # es demasiado texto para una celda de tabla y, sin acotar, vuelve
        # lento el resaltado (mark_fragment/highlight) de las CIENTOS de
        # apariciones que se dibujan aquí. Se reemplaza por el mismo
        # recorte ±50 palabras alrededor de la keyword que ya usa la vista
        # de detalle (_enrich_match), solo para lo que realmente se muestra.
        phonetic, whole_word = bool(s['phonetic']), bool(s['whole_word'])
        for c in shown:
            for o in c['occurrences']:
                o['matched_text'] = _enrich_match(o, phonetic=phonetic, whole_word=whole_word)['centered_text']
        for o in unique_shown:
            o['matched_text'] = _enrich_match(o, phonetic=phonetic, whole_word=whole_word)['centered_text']

        report = dict(report, clusters=shown, unique_matches=unique_shown)

        return render_template('search_similarities.html', s=s, report=report,
                               show_pairs=show_pairs, pairs_hidden=pairs_hidden,
                               truncated=truncated, unique_truncated=unique_truncated)

    def _match_moment(sid, mid):
        """Devuelve (match_row, datetime del instante exacto de la palabra) o (None, None)."""
        s = _get_search(sid)
        if not s:
            return None, None
        m = db().execute(
            "SELECT * FROM matches WHERE id=? AND search_id=?", (mid, sid)
        ).fetchone()
        if not m:
            return None, None
        md = _enrich_match(m, phonetic=bool(s['phonetic']), whole_word=bool(s['whole_word']))
        try:
            moment = datetime.fromisoformat(md['precise_timestamp'])
        except Exception:
            return m, None
        return m, moment

    @app.route('/searches/<int:sid>/matches/<int:mid>/snapshot.jpg')
    @login_required
    def match_snapshot(sid, mid):
        from alerts.clips import extract_snapshot, CACHE_DIR
        m, moment = _match_moment(sid, mid)
        if not m or not moment:
            return ('', 404)
        out = CACHE_DIR / f'{mid}.jpg'
        if not out.exists():
            if not extract_snapshot(m['channel_name'] or '', moment, out):
                return ('', 404)
        return send_file(out, mimetype='image/jpeg')

    @app.route('/searches/<int:sid>/matches/<int:mid>/clip.mp4')
    @login_required
    def match_clip(sid, mid):
        from alerts.clips import extract_clip, CACHE_DIR
        m, moment = _match_moment(sid, mid)
        if not m or not moment:
            return ('', 404)
        out = CACHE_DIR / f'{mid}.mp4'
        if not out.exists():
            if not extract_clip(m['channel_name'] or '', moment, out):
                return ('', 404)
        return send_file(out, mimetype='video/mp4')

    @app.route('/searches/<int:sid>/matches/<int:mid>/audio_clip.m4a')
    @login_required
    def match_audio_clip(sid, mid):
        """Clip de audio (±10s) centrado en una coincidencia de radio --
        misma idea que match_clip para TV, pero recortado de las
        grabaciones de radio (local o NAS, ver alerts/audio_clips.py)."""
        from alerts.audio_clips import extract_clip, CACHE_DIR
        m, moment = _match_moment(sid, mid)
        if not m or not moment or m['channel_id'] is None:
            return ('', 404)
        out = CACHE_DIR / f'{mid}.m4a'
        if not out.exists():
            if not extract_clip(m['channel_id'], moment, out):
                return ('', 404)
        return send_file(out, mimetype='audio/mp4')

    @app.route('/searches/<int:sid>/matches/<int:mid>/full_media')
    @login_required
    def match_full_media(sid, mid):
        """URL + segundo exacto para reproducir la grabación COMPLETA de 30
        min (no el clip recortado de ±10s) -- así se puede adelantar/atrasar
        libremente en vez de quedar atado a la ventana fija. El JS del modal
        pide esto primero, luego pone src=url y currentTime=offset."""
        from alerts.channel_types import channel_type
        m, moment = _match_moment(sid, mid)
        if not m or not moment:
            return jsonify(error='not found'), 404
        kind = channel_type(m['channel_id'])
        if kind == 'tv':
            from alerts.clips import locate_frame
            found = locate_frame(m['channel_name'] or '', moment)
            if not found:
                return jsonify(error='not found'), 404
            _, offset = found
            return jsonify(kind='tv', url=f'/searches/{sid}/matches/{mid}/full_video.mp4', offset=offset)
        elif kind == 'radio':
            from alerts.audio_clips import locate_segment
            if m['channel_id'] is None:
                return jsonify(error='not found'), 404
            found = locate_segment(m['channel_id'], moment)
            if not found:
                return jsonify(error='not found'), 404
            _, offset = found
            return jsonify(kind='radio', url=f'/searches/{sid}/matches/{mid}/full_audio.m4a', offset=offset)
        return jsonify(error='not applicable'), 404

    @app.route('/searches/<int:sid>/matches/<int:mid>/full_video.mp4')
    @login_required
    def match_full_video(sid, mid):
        """Sirve el bloque de 30 min COMPLETO (no el clip recortado) --
        mismo remux/caché que la Videoteca (get_or_build_clip), así que
        .ts/.mkv en vivo también se sirven reproducibles."""
        from alerts.clips import locate_frame
        from alerts.library import get_or_build_clip
        m, moment = _match_moment(sid, mid)
        if not m or not moment or m['channel_id'] is None:
            return ('', 404)
        found = locate_frame(m['channel_name'] or '', moment)
        if not found:
            return ('', 404)
        path, _ = found
        clip = get_or_build_clip(m['channel_id'], path.parent, path.name)
        if clip is None:
            return ('', 404)
        return send_file(clip, mimetype='video/mp4', conditional=True)

    @app.route('/searches/<int:sid>/matches/<int:mid>/full_audio.m4a')
    @login_required
    def match_full_audio(sid, mid):
        """Sirve el bloque de 30 min COMPLETO de audio (no el clip
        recortado) -- ya es .aac reproducible directo, sin remux."""
        from alerts.audio_clips import locate_segment
        m, moment = _match_moment(sid, mid)
        if not m or not moment or m['channel_id'] is None:
            return ('', 404)
        found = locate_segment(m['channel_id'], moment)
        if not found:
            return ('', 404)
        path, _ = found
        return send_file(path, mimetype='audio/aac', conditional=True)

    @app.route('/searches/<int:sid>/matches/<int:mid>/delete', methods=['POST'])
    @login_required
    def delete_match(sid, mid):
        """Borra manualmente una coincidencia que no tiene relación real con
        la búsqueda (falso positivo, ej. del modo fonético) -- no toca la
        transcripción original ni afecta detecciones futuras, solo esta fila."""
        s = _get_search(sid)
        if not s:
            return jsonify(error='not found'), 404
        cur = db().execute("DELETE FROM matches WHERE id=? AND search_id=?", (mid, sid))
        db().commit()
        if cur.rowcount == 0:
            return jsonify(error='not found'), 404
        return jsonify(ok=True)

    # ══════════════════════════════════════════════════════════════
    # MONITOR DE SEÑALES (mosaico en vivo)
    # ══════════════════════════════════════════════════════════════
    @app.route('/videowall')
    @login_required
    def videowall():
        import math
        from alerts.videowall import list_channels
        channels = list_channels()
        cols = 6
        rows = max(1, math.ceil(len(channels) / cols)) if channels else 1
        return render_template('videowall.html', channels=channels, cols=cols, rows=rows)

    @app.route('/videowall/thumb/<int:num>.jpg')
    @login_required
    def videowall_thumb(num):
        from alerts.videowall import list_channels, get_thumbnail
        ch = next((c for c in list_channels() if c['num'] == num), None)
        if ch is None:
            return ('', 404)
        out = get_thumbnail(num, ch['folder'])
        if out is None:
            return ('', 404)
        resp = send_file(out, mimetype='image/jpeg')
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    @app.route('/videowall/stream/<int:num>')
    @login_required
    def videowall_stream(num):
        """MJPEG en vivo (multipart/x-mixed-replace) — requiere threaded=True
        en app.run() (ver run_alerts.py), esta conexión se mantiene abierta
        indefinidamente mientras la pestaña esté abierta."""
        from flask import Response
        from alerts.videowall import list_channels, stream_mjpeg
        ch = next((c for c in list_channels() if c['num'] == num), None)
        if ch is None:
            return ('', 404)
        width = request.args.get('w', 640, type=int)
        resp = Response(stream_mjpeg(ch['folder'], width=width),
                         mimetype='multipart/x-mixed-replace; boundary=ffmpegframe')
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    @app.route('/videowall/av/<int:num>')
    @login_required
    def videowall_av(num):
        """Video+audio EN VIVO (MP4 fragmentado h264/aac, conectado directo a
        TVHeadend) de UN canal — para la vista ampliada, consumido por un
        <video> nativo del navegador. Solo un canal a la vez (no el mosaico
        completo, que sigue leyendo de la grabación local -- ver el
        docstring de alerts/videowall.py)."""
        from flask import Response
        from alerts.videowall import list_channels, stream_live_av
        ch = next((c for c in list_channels() if c['num'] == num), None)
        if ch is None:
            return ('', 404)
        # Sin default: resolución completa (la fuente cruda de TVHeadend es
        # 1080p) salvo que se pida explícitamente un ancho menor por query param.
        width = request.args.get('w', type=int)
        resp = Response(stream_live_av(num, width=width), mimetype='video/mp4')
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    @app.route('/videowall/stream/all')
    @login_required
    def videowall_stream_all():
        """MJPEG en vivo de TODOS los canales compuestos en una sola grilla —
        un solo stream HTTP para toda la página, evitando el límite de 6
        conexiones simultáneas por origen que tienen los navegadores (con un
        <img> por canal, los primeros 6 acaparan el cupo para siempre)."""
        from flask import Response
        from alerts.videowall import list_channels, stream_wall_mjpeg
        resp = Response(stream_wall_mjpeg(list_channels()),
                         mimetype='multipart/x-mixed-replace; boundary=ffmpegframe')
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    @app.route('/radiowall')
    @login_required
    def radiowall():
        from alerts.radiowall import list_radio_stations
        return render_template('radiowall.html', stations=list_radio_stations())

    @app.route('/radiowall/stream/<int:num>')
    @login_required
    def radiowall_stream(num):
        """Proxy del audio en vivo de una estación — necesario porque algunas
        (detrás de Zeno.fm) exigen un header Origin que un <audio> del
        navegador no puede mandar; ver alerts/radiowall.py."""
        from flask import Response
        from alerts.radiowall import stream_proxy
        gen, content_type = stream_proxy(num)
        if gen is None:
            return ('', 502)
        resp = Response(gen, mimetype=content_type)
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    @app.route('/library')
    @login_required
    def library():
        from alerts.library import list_channels
        return render_template('library.html', channels=list_channels())

    @app.route('/library/<int:num>')
    @login_required
    def library_channel(num):
        from alerts.library import get_channel, list_dates, list_blocks
        ch = get_channel(num)
        if ch is None:
            return ('', 404)
        dates = list_dates(ch)
        date = request.args.get('date') or (dates[0] if dates else None)
        blocks = list_blocks(ch, date, epg_db=db()) if date else []
        return render_template('library_channel.html', channel=ch, dates=dates,
                                date=date, blocks=blocks)

    @app.route('/library/video/<int:num>/<filename>')
    @login_required
    def library_video(num, filename):
        """Sirve un bloque de 30 min como MP4 reproducible -- directo si ya
        está finalizado, o remuxeado (sin recodificar, cacheado la primera
        vez) si todavía es .ts/.mkv. send_file soporta Range de forma nativa
        -- permite adelantar/atrasar el video sin volver a descargarlo entero."""
        from alerts.library import get_channel, get_or_build_clip, _SEG_RE
        if not _SEG_RE.search(filename) or '/' in filename or '..' in filename:
            return ('', 400)
        ch = get_channel(num)
        if ch is None:
            return ('', 404)
        clip = get_or_build_clip(num, ch['folder'], filename)
        if clip is None:
            return ('', 404)
        return send_file(clip, mimetype='video/mp4', conditional=True)

    @app.route('/library/download/<int:num>/<filename>')
    @login_required
    def library_download(num, filename):
        """Descarga el archivo NATIVO del bloque (.mp4 finalizado, o .ts si
        aún no se ha finalizado) -- sin transcodificar, aunque el canal sea
        GPU/AV1: los reproductores de escritorio (VLC, etc.) lo reproducen
        bien aunque el navegador no lo soporte de forma nativa."""
        from alerts.library import get_channel, _SEG_RE, _nas_path_for
        if not _SEG_RE.search(filename) or '/' in filename or '..' in filename:
            return ('', 400)
        ch = get_channel(num)
        if ch is None:
            return ('', 404)
        path = ch['folder'] / filename
        if not path.exists():
            # Ya no está en disco local (cleanup_video.py) -- se busca en el
            # respaldo del NAS antes de dar 404.
            nas_path = _nas_path_for(filename)
            path = nas_path if nas_path and nas_path.exists() else None
        if path is None:
            return ('', 404)
        return send_file(path, as_attachment=True, download_name=filename)

    @app.route('/audio-library')
    @login_required
    def audio_library():
        from alerts.audio_library import list_stations
        return render_template('audio_library.html', stations=list_stations())

    @app.route('/audio-library/<int:num>')
    @login_required
    def audio_library_station(num):
        from alerts.audio_library import get_station, list_dates, list_blocks
        st = get_station(num)
        if st is None:
            return ('', 404)
        dates = list_dates(num)
        date = request.args.get('date') or (dates[0] if dates else None)
        blocks = list_blocks(num, date) if date else []
        return render_template('audio_library_station.html', station=st, dates=dates,
                                date=date, blocks=blocks)

    @app.route('/audio-library/play/<int:num>/<filename>')
    @login_required
    def audio_library_play(num, filename):
        """Sirve un bloque de 30 min de audio -- local (el más reciente) o
        NAS, sin transcodificar. send_file soporta Range de forma nativa --
        permite adelantar/atrasar sin volver a descargar el bloque entero."""
        from alerts.audio_library import get_station, resolve_block
        if get_station(num) is None:
            return ('', 404)
        path = resolve_block(num, filename)
        if path is None:
            return ('', 404)
        return send_file(path, mimetype='audio/aac', conditional=True)

    @app.route('/audio-library/download/<int:num>/<filename>')
    @login_required
    def audio_library_download(num, filename):
        from alerts.audio_library import get_station, resolve_block
        if get_station(num) is None:
            return ('', 404)
        path = resolve_block(num, filename)
        if path is None:
            return ('', 404)
        return send_file(path, as_attachment=True, download_name=filename)

    @app.route('/transcript-archive')
    @login_required
    def transcript_archive():
        from alerts.transcript_archive import list_channels
        return render_template('transcript_archive.html', channels=list_channels())

    @app.route('/transcript-archive/<int:channel_id>')
    @login_required
    def transcript_archive_channel(channel_id):
        from alerts.transcript_archive import get_channel_name, list_dates, list_chunks
        name = get_channel_name(channel_id)
        if name is None:
            return ('', 404)
        dates = list_dates(channel_id)
        date = request.args.get('date') or (dates[0] if dates else None)
        chunks = list_chunks(channel_id, date) if date else []
        return render_template('transcript_archive_channel.html', channel_id=channel_id,
                                channel_name=name, dates=dates, date=date, chunks=chunks)

    @app.route('/searches/<int:sid>/edit', methods=['GET', 'POST'])
    @login_required
    def search_edit(sid):
        s = _get_search(sid)
        if not s:
            flash('Búsqueda no encontrada.', 'danger')
            return redirect(url_for('dashboard'))

        if request.method == 'POST':
            name      = request.form.get('name', '').strip()
            kw_raw    = request.form.get('keywords', '').strip()
            excl_raw  = request.form.get('exclude_words', '').strip()
            kws       = [k.strip() for k in re.split(r'[\n,]+', kw_raw) if k.strip()]
            excl      = [e.strip() for e in re.split(r'[\n,]+', excl_raw) if e.strip()]
            notify_tg  = 1 if request.form.get('notify_telegram') else 0
            new_start  = request.form.get('date_start')
            new_end    = request.form.get('date_end')
            new_kws    = json.dumps(kws, ensure_ascii=False)
            new_excl   = json.dumps(excl, ensure_ascii=False)
            new_phon   = 1 if request.form.get('phonetic') else 0
            new_whole  = 1 if request.form.get('whole_word') else 0
            new_media  = ','.join(request.form.getlist('media_types')) or 'tv,radio'
            # No entra en needs_reinit: solo cambia cómo se cuentan las coincidencias
            # DE AQUÍ EN ADELANTE, no reinterpreta lo ya escaneado.
            new_dedup  = 1 if request.form.get('dedup_channel') else 0
            new_exclude_music = 1 if request.form.get('exclude_music') else 0
            new_threshold_enabled = 1 if request.form.get('threshold_alert_enabled') else 0
            new_threshold_count   = int(request.form.get('threshold_count') or 5)
            new_threshold_window  = int(request.form.get('threshold_window_min') or 30)
            new_weekly_excel      = 1 if request.form.get('weekly_excel_report') else 0

            # Si cambian fechas, palabras, exclusiones, tipo de búsqueda, medios
            # o el filtro de música → re-escanear histórico (exclude_music SÍ
            # cambia qué filas califican, a diferencia de dedup_channel).
            needs_reinit = (
                new_start != s['date_start']  or
                new_end   != s['date_end']    or
                new_kws   != s['keywords']    or
                new_excl  != (s['exclude_words'] if 'exclude_words' in s.keys() and s['exclude_words'] else '[]') or
                new_phon  != s['phonetic']    or
                new_whole != s['whole_word']  or
                new_media != (s['media_types'] if 'media_types' in s.keys() else 'tv,radio') or
                new_exclude_music != (s['exclude_music'] if 'exclude_music' in s.keys() and s['exclude_music'] is not None else 1)
            )

            db().execute("""
                UPDATE searches SET
                  name=?, keywords=?, exclude_words=?, phonetic=?, whole_word=?, date_start=?, date_end=?,
                  delivery_mode=?, report_email=?, status=?, notify_telegram=?,
                  initialized=?, media_types=?, dedup_channel=?, exclude_music=?,
                  threshold_alert_enabled=?, threshold_count=?, threshold_window_min=?,
                  weekly_excel_report=?
                WHERE id=?
            """, (name, new_kws, new_excl, new_phon, new_whole, new_start, new_end,
                  request.form.get('delivery_mode', 'final'),
                  request.form.get('report_email', '').strip(),
                  request.form.get('status', 'active'), notify_tg,
                  0 if needs_reinit else s['initialized'], new_media, new_dedup, new_exclude_music,
                  new_threshold_enabled, new_threshold_count, new_threshold_window, new_weekly_excel,
                  sid))

            if needs_reinit:
                db().execute("DELETE FROM matches WHERE search_id=?", (sid,))
                flash('Búsqueda actualizada. Re-escaneando histórico con el nuevo rango…', 'success')
            else:
                flash('Búsqueda actualizada.', 'success')

            db().commit()
            return redirect(url_for('search_detail', sid=sid))

        from alerts.channel_types import MEDIA_TYPES
        current_media = (s['media_types'] if 'media_types' in s.keys() and s['media_types'] else 'tv,radio').split(',')
        return render_template('search_edit.html', s=s,
                               keywords=json.loads(s['keywords']),
                               exclude_words=json.loads(s['exclude_words']) if 'exclude_words' in s.keys() and s['exclude_words'] else [],
                               media_types_choices=MEDIA_TYPES,
                               current_media_types=current_media)

    @app.route('/searches/<int:sid>/toggle', methods=['POST'])
    @login_required
    def search_toggle(sid):
        s = _get_search(sid)
        if s:
            ns = 'paused' if s['status'] == 'active' else 'active'
            db().execute("UPDATE searches SET status=? WHERE id=?", (ns, sid))
            db().commit()
        return redirect(request.referrer or url_for('dashboard'))

    @app.route('/searches/<int:sid>/delete', methods=['POST'])
    @login_required
    def search_delete(sid):
        s = _get_search(sid)
        if s:
            db().execute("DELETE FROM matches  WHERE search_id=?", (sid,))
            db().execute("DELETE FROM searches WHERE id=?",        (sid,))
            db().commit()
            flash('Búsqueda eliminada.', 'success')
        return redirect(url_for('dashboard'))

    @app.route('/searches/<int:sid>/report', methods=['POST'])
    @login_required
    def search_report(sid):
        from alerts.mailer import send_report
        s = _get_search(sid)
        if not s:
            flash('Búsqueda no encontrada.', 'danger')
            return redirect(url_for('dashboard'))
        kfs       = request.form.getlist('kw')
        cfs       = request.form.getlist('ch')
        pfs       = request.form.getlist('prog')
        mfs       = request.form.getlist('mt')
        date_from = request.form.get('date_from', '')
        date_to   = request.form.get('date_to', '')
        where, params = _match_where(sid, kfs, cfs, pfs, mfs, date_from, date_to)
        d       = db()
        matches = d.execute(f"SELECT * FROM matches WHERE {where} ORDER BY timestamp DESC", params).fetchall()
        rows    = d.execute("SELECT key,value FROM settings").fetchall()
        cfg     = {r['key']: r['value'] for r in rows}
        if not cfg.get('smtp_host') and not cfg.get('gmail_refresh_token'):
            flash('Configura el servidor SMTP o Gmail API primero.', 'warning')
        else:
            ok, msg = send_report(s, [dict(m) for m in matches], cfg, 'manual')
            flash(msg, 'success' if ok else 'danger')
        return redirect(url_for('search_detail', sid=sid))

    @app.route('/searches/<int:sid>/report_telegram', methods=['POST'])
    @login_required
    def search_report_telegram(sid):
        from alerts.telegram import send_report as tg_send_report
        s = _get_search(sid)
        if not s:
            flash('Búsqueda no encontrada.', 'danger')
            return redirect(url_for('dashboard'))
        kfs       = request.form.getlist('kw')
        cfs       = request.form.getlist('ch')
        pfs       = request.form.getlist('prog')
        mfs       = request.form.getlist('mt')
        date_from = request.form.get('date_from', '')
        date_to   = request.form.get('date_to', '')
        where, params = _match_where(sid, kfs, cfs, pfs, mfs, date_from, date_to)
        d       = db()
        matches = d.execute(f"SELECT * FROM matches WHERE {where} ORDER BY timestamp DESC", params).fetchall()
        cfg     = {r['key']: r['value'] for r in d.execute("SELECT key,value FROM settings")}
        token   = cfg.get('tg_token', '')
        # Usa el chat_id personal del dueño de la búsqueda; fallback al global
        owner   = d.execute("SELECT tg_chat_id FROM users WHERE id=?", (s['user_id'],)).fetchone()
        chat_id = (owner['tg_chat_id'] if owner else '') or cfg.get('tg_chat_id', '')
        if not token or not chat_id:
            flash('Configura tu Chat ID de Telegram en Mi perfil primero.', 'warning')
        else:
            ok, err = tg_send_report(token, chat_id, dict(s), [dict(m) for m in matches])
            flash('Reporte enviado a Telegram.' if ok else f'Error Telegram: {err}',
                  'success' if ok else 'danger')
        return redirect(url_for('search_detail', sid=sid))

    @app.route('/settings/test_smtp', methods=['POST'])
    @admin_required
    def settings_test_smtp():
        from alerts.mailer import test_connection
        d   = db()
        cfg = {r['key']: r['value'] for r in d.execute("SELECT key,value FROM settings")}
        to  = cfg.get('gmail_authorized_email', '') if cfg.get('gmail_refresh_token') else cfg.get('smtp_user', '')
        if not cfg.get('gmail_refresh_token') and not cfg.get('smtp_host'):
            flash('Configura el servidor SMTP o autoriza Gmail API antes de probar.', 'warning')
        elif not to:
            flash('Falta el correo de destino de la prueba.', 'warning')
        else:
            ok, msg = test_connection(cfg, to)
            flash(msg, 'success' if ok else 'danger')
        return redirect(url_for('settings'))

    @app.route('/settings/test_telegram', methods=['POST'])
    @admin_required
    def settings_test_telegram():
        from alerts.telegram import test_connection
        d       = db()
        cfg     = {r['key']: r['value'] for r in d.execute("SELECT key,value FROM settings")}
        token   = cfg.get('tg_token', '')
        chat_id = cfg.get('tg_chat_id', '')
        if not token or not chat_id:
            flash('Ingresa el Token y Chat ID antes de probar.', 'warning')
        else:
            ok, err = test_connection(token, chat_id)
            flash('Mensaje de prueba enviado correctamente.' if ok else f'Error: {err}',
                  'success' if ok else 'danger')
        return redirect(url_for('settings'))

    # ══════════════════════════════════════════════════════════════
    # ADMIN
    # ══════════════════════════════════════════════════════════════
    @app.route('/admin')
    @admin_required
    def admin():
        d = db()
        users = d.execute("""
            SELECT u.*,
                   (SELECT COUNT(*) FROM searches s WHERE s.user_id=u.id) sc
            FROM users u ORDER BY u.created_at DESC
        """).fetchall()
        searches = d.execute("""
            SELECT s.*, u.name as u_name,
                   (SELECT COUNT(*) FROM matches m WHERE m.search_id=s.id) mc
            FROM searches s JOIN users u ON s.user_id=u.id
            ORDER BY s.created_at DESC
        """).fetchall()
        return render_template('admin.html', users=users, searches=searches)

    def _read_json_status(path):
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    @app.route('/admin/startup')
    @admin_required
    def admin_startup():
        """Progreso/resultado del último arranque escalonado (ver
        startup_sequence.py, corrido por systemd al iniciar la máquina) --
        para poder revisarlo desde el navegador en vez de solo por
        journalctl. Si esta página misma está respondiendo, el arranque ya
        llegó al menos hasta levantar alerts.service (uno de los últimos
        pasos), así que un arranque roto ANTES de eso no se puede ver
        aquí -- solo por journalctl -u alertatv-startup.service.

        Muestra también la última prueba de salud manual (ver
        system_health.py) -- son dos archivos separados a propósito: correr
        una prueba de salud NO debe borrar el registro de cómo salió el
        último arranque real de la máquina, son preguntas distintas ("¿cómo
        arrancó?" vs "¿cómo está ahora mismo?")."""
        startup_status = _read_json_status(BASE_DIR / "logs" / "startup_status.json")
        health_status = _read_json_status(BASE_DIR / "logs" / "health_check_status.json")
        return render_template('admin_startup.html', status=startup_status, health=health_status)

    @app.route('/admin/startup/health_check', methods=['POST'])
    @admin_required
    def admin_startup_health_check():
        """Corre la prueba de salud AHORA MISMO sobre lo que ya está
        corriendo -- no inicia ni reinicia nada, solo verifica (ver
        system_health.run_full_health_check). Tarda ~15s (espera corta
        para confirmar que TV/radio de verdad siguen transcribiendo), así
        que esta petición se queda esperando esa respuesta -- el botón en
        la plantilla se deshabilita mientras tanto para no disparar dos
        corridas encimadas."""
        from system_health import run_full_health_check
        run_full_health_check()
        return redirect(url_for('admin_startup'))

    @app.route('/admin/health')
    @admin_required
    def admin_health():
        from datetime import datetime
        today = datetime.now().strftime('%Y-%m-%d')
        return redirect(url_for('admin_health_date', date_str=today))

    @app.route('/admin/health/<date_str>')
    @admin_required
    def admin_health_date(date_str):
        from datetime import datetime, timedelta
        from alerts.health_report import get_or_generate, list_report_dates, live_status
        today = datetime.now().strftime('%Y-%m-%d')
        report = get_or_generate(date_str)
        d = datetime.strptime(date_str, '%Y-%m-%d')
        return render_template('admin_health.html', report=report, date_str=date_str,
                               is_today=(date_str == today), dates=list_report_dates(),
                               # Estado en vivo: siempre "ahora", sin importar que dia se este
                               # navegando -- responde "como estamos AHORA MISMO", no el historico.
                               live=live_status(),
                               prev_date=(d - timedelta(days=1)).strftime('%Y-%m-%d'),
                               next_date=(d + timedelta(days=1)).strftime('%Y-%m-%d') if date_str < today else None)

    @app.route('/admin/users/<int:uid>/toggle', methods=['POST'])
    @admin_required
    def admin_user_toggle(uid):
        if uid != session['uid']:
            u = db().execute("SELECT active FROM users WHERE id=?", (uid,)).fetchone()
            if u:
                db().execute("UPDATE users SET active=? WHERE id=?",
                             (0 if u['active'] else 1, uid))
                db().commit()
        return redirect(url_for('admin'))

    @app.route('/admin/users/<int:uid>/role', methods=['POST'])
    @admin_required
    def admin_user_role(uid):
        if uid != session['uid']:
            role = request.form.get('role', 'user')
            db().execute("UPDATE users SET role=? WHERE id=?", (role, uid))
            db().commit()
        return redirect(url_for('admin'))

    @app.route('/admin/corrections', methods=['GET', 'POST'])
    @admin_required
    def admin_corrections():
        """Diccionario de corrección post-transcripción (ver
        text_corrections.py) -- pensado sobre todo para radio (Parakeet-TDT
        no tiene un mecanismo de refuerzo de vocabulario seguro, ver
        proto_boosting/). Toma efecto solo/en un minuto en los motores en
        vivo (cache de 60s en text_corrections.py) -- no requiere
        reiniciar ningún servicio."""
        d = db()
        if request.method == 'POST':
            pattern     = request.form.get('pattern', '').strip()
            replacement = request.form.get('replacement', '').strip()
            if pattern and replacement:
                d.execute("INSERT INTO text_corrections (pattern, replacement) VALUES (?,?)",
                          (pattern, replacement))
                d.commit()
                # Aplica de una vez a lo YA transcrito (no solo a lo nuevo de
                # aquí en adelante) -- a pedido explícito, para no depender
                # de correr un script aparte cada vez que se agrega una.
                from text_corrections import apply_to_history
                result = apply_to_history(pattern, replacement)
                flash(f'Corrección agregada: "{pattern}" → "{replacement}". '
                      f'Se corrigieron {result["total"]} fragmento(s) ya transcritos.', 'success')
            return redirect(url_for('admin_corrections'))
        corrections = d.execute("SELECT * FROM text_corrections ORDER BY created_at DESC").fetchall()
        return render_template('admin_corrections.html', corrections=corrections)

    @app.route('/admin/corrections/reapply', methods=['POST'])
    @admin_required
    def admin_corrections_reapply():
        """Reaplica TODAS las correcciones activas a lo ya transcrito --
        útil tras corregir un error en el mecanismo mismo, o solo para
        verificar de nuevo. Agregar una corrección nueva ya la aplica sola
        (ver admin_corrections), esto es para el resto."""
        from text_corrections import apply_to_history
        result = apply_to_history()
        flash(f'Listo: {result["total"]} fragmento(s) corregidos en total, en '
              f'{len(result["por_correccion"])} corrección(es) revisada(s).', 'success')
        return redirect(url_for('admin_corrections'))

    @app.route('/admin/corrections/<int:cid>/delete', methods=['POST'])
    @admin_required
    def admin_corrections_delete(cid):
        db().execute("DELETE FROM text_corrections WHERE id=?", (cid,))
        db().commit()
        return redirect(url_for('admin_corrections'))

    BOOSTED_WORDS_FILE = BASE_DIR / "models" / "parakeet-ctc-es" / "boosted_words.txt"
    BOOSTED_WORDS_HEADER = (
        "# Palabras/frases a reforzar en la transcripción de TV (CTC-ES).\n"
        "# Una por línea. Líneas que empiezan con # se ignoran.\n"
        "# Requiere reiniciar transcriber-ctc-es.service para que un cambio aquí tome efecto.\n"
    )

    def _read_boosted_words() -> list[str]:
        if not BOOSTED_WORDS_FILE.exists():
            return []
        return [line.strip() for line in BOOSTED_WORDS_FILE.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")]

    def _write_boosted_words(words: list[str]) -> None:
        BOOSTED_WORDS_FILE.parent.mkdir(parents=True, exist_ok=True)
        BOOSTED_WORDS_FILE.write_text(BOOSTED_WORDS_HEADER + "\n".join(words) + ("\n" if words else ""),
                                       encoding="utf-8")

    @app.route('/admin/boosted_words', methods=['GET', 'POST'])
    @admin_required
    def admin_boosted_words():
        """Lista de palabras a reforzar para TV (ver ctc_es_boosting.py) --
        a diferencia de /admin/corrections (radio), este mecanismo SÍ
        requiere reiniciar transcriber-ctc-es.service para tomar efecto: el
        grafo de refuerzo se construye una sola vez al cargar el modelo, no
        se puede recargar en caliente. Por eso no se dispara el reinicio
        aquí mismo -- el admin decide cuándo, igual que cualquier otro
        cambio a los motores de transcripción en vivo."""
        if request.method == 'POST':
            word = request.form.get('word', '').strip()
            words = _read_boosted_words()
            if word and word not in words:
                words.append(word)
                _write_boosted_words(words)
                flash(f'"{word}" agregada. Reinicia transcriber-ctc-es.service para que tome efecto.', 'success')
            return redirect(url_for('admin_boosted_words'))
        return render_template('admin_boosted_words.html', words=_read_boosted_words())

    @app.route('/admin/boosted_words/delete', methods=['POST'])
    @admin_required
    def admin_boosted_words_delete():
        word = request.form.get('word', '')
        words = [w for w in _read_boosted_words() if w != word]
        _write_boosted_words(words)
        flash(f'"{word}" eliminada. Reinicia transcriber-ctc-es.service para que tome efecto.', 'success')
        return redirect(url_for('admin_boosted_words'))

    # ══════════════════════════════════════════════════════════════
    # SETTINGS (SMTP)
    # ══════════════════════════════════════════════════════════════
    @app.route('/settings', methods=['GET', 'POST'])
    @admin_required
    def settings():
        from alerts.epg import get_coverage_stats
        d = db()
        if request.method == 'POST':
            for k in ['smtp_host','smtp_port','smtp_user','smtp_pass','smtp_from','smtp_tls',
                      'gmail_client_id','gmail_client_secret',
                      'tg_token','tg_chat_id']:
                d.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
                          (k, request.form.get(k, '')))
            d.commit()
            flash('Configuración guardada.', 'success')
        cfg       = {r['key']: r['value'] for r in d.execute("SELECT key,value FROM settings")}
        epg_stats = get_coverage_stats(d)
        return render_template('settings.html', cfg=cfg, epg_stats=epg_stats)

    @app.route('/settings/epg_fetch', methods=['POST'])
    @admin_required
    def settings_epg_fetch():
        from alerts.epg import fetch_current
        n = fetch_current()
        db().execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('epg_last_fetch',?)",
                     (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),))
        db().commit()
        flash(f'EPG actualizado: {n} programas nuevos guardados.', 'success')
        return redirect(url_for('settings'))

    # ══════════════════════════════════════════════════════════════
    # API JSON (para auto-refresh)
    # ══════════════════════════════════════════════════════════════
    @app.route('/api/searches/<int:sid>/matches')
    @login_required
    def api_matches(sid):
        s = _get_search(sid)
        if not s:
            return jsonify(error='not found'), 404
        d        = db()
        since    = request.args.get('since', '')
        after_id = request.args.get('after_id', type=int)

        # Mismos filtros que la vista principal (kw/ch/prog/mt/fechas) -- sin
        # esto, el auto-refresh cada 8s (ver search_detail.html refresh())
        # inyectaba CUALQUIER coincidencia nueva sin importar el filtro activo
        # (ej. filtrar por "Televisión" y ver aparecer coincidencias de radio).
        kfs       = request.args.getlist('kw')
        cfs       = request.args.getlist('ch')
        pfs       = request.args.getlist('prog')
        mfs       = request.args.getlist('mt')
        date_from = request.args.get('date_from', '')
        date_to   = request.args.get('date_to', '')
        where, params = _match_where(sid, kfs, cfs, pfs, mfs, date_from, date_to)

        if after_id is not None:
            rows = d.execute(
                f"SELECT * FROM matches WHERE {where} AND id>? ORDER BY id ASC LIMIT 100",
                (*params, after_id)
            ).fetchall()
        elif since:
            rows = d.execute(
                f"SELECT * FROM matches WHERE {where} AND found_at>? ORDER BY found_at DESC LIMIT 50",
                (*params, since)
            ).fetchall()
        else:
            rows = d.execute(
                f"SELECT * FROM matches WHERE {where} ORDER BY found_at DESC LIMIT 50", params
            ).fetchall()
        total = d.execute(f"SELECT COUNT(*) FROM matches WHERE {where}", params).fetchone()[0]
        phonetic   = bool(s['phonetic'])
        whole_word = bool(s['whole_word'])
        enriched = [_enrich_match(r, phonetic=phonetic, whole_word=whole_word) for r in rows]
        for m in enriched:
            # HTML ya resaltado con la misma lógica que el render inicial (filtro
            # Jinja "highlight") -- el JS de auto-refresh solo lo inserta tal cual,
            # en vez de re-implementar el resaltado (y perder el modo fonético).
            m['centered_highlighted'] = str(_highlight(m.get('centered_text', ''), m.get('keyword', ''),
                                                        phonetic=phonetic, whole_word=whole_word))
            m['matched_highlighted'] = str(_highlight(m.get('matched_text', ''), m.get('keyword', ''),
                                                       phonetic=phonetic, whole_word=whole_word))
        return jsonify(matches=enriched, total=total)

    @app.route('/api/stats')
    @login_required
    def api_stats():
        d        = db()
        uid      = session['uid']
        is_admin = session.get('role') == 'admin'

        if is_admin:
            searches = d.execute("""
                SELECT s.id, s.name, s.status, s.initialized, s.init_rows_done, s.init_rows_total,
                       (SELECT COUNT(*) FROM matches m WHERE m.search_id=s.id) as mc
                FROM searches s ORDER BY s.created_at DESC
            """).fetchall()
            recent = d.execute("""
                SELECT m.id, m.keyword, m.channel_name, m.timestamp, m.matched_text,
                       s.name as s_name, s.id as s_id
                FROM matches m
                JOIN searches s ON m.search_id=s.id
                ORDER BY m.timestamp DESC LIMIT 25
            """).fetchall()
            total_matches = d.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        else:
            searches = d.execute("""
                SELECT s.id, s.name, s.status, s.initialized, s.init_rows_done, s.init_rows_total,
                       (SELECT COUNT(*) FROM matches m WHERE m.search_id=s.id) as mc
                FROM searches s WHERE s.user_id=? ORDER BY s.created_at DESC
            """, (uid,)).fetchall()
            recent = d.execute("""
                SELECT m.id, m.keyword, m.channel_name, m.timestamp, m.matched_text,
                       s.name as s_name, s.id as s_id
                FROM matches m JOIN searches s ON m.search_id=s.id
                WHERE s.user_id=? ORDER BY m.timestamp DESC LIMIT 25
            """, (uid,)).fetchall()
            total_matches = sum(s['mc'] for s in searches)

        active_searches = sum(1 for s in searches if s['status'] == 'active')

        def _pct(s):
            total = s['init_rows_total'] or 0
            done  = s['init_rows_done'] or 0
            return round(done / total * 100) if total > 0 else 0

        return jsonify(
            searches=[{'id': s['id'], 'mc': s['mc'], 'initialized': bool(s['initialized']),
                       'init_pct': _pct(s)} for s in searches],
            total_matches=total_matches,
            active_searches=active_searches,
            recent=[{
                'id':           m['id'],
                'keyword':      m['keyword'],
                'channel_name': m['channel_name'],
                'timestamp':    m['timestamp'],
                'matched_text': m['matched_text'],
                's_name':       m['s_name'],
                's_id':         m['s_id'],
            } for m in recent],
        )

    @app.route('/api/searches/<int:sid>/progress')
    @login_required
    def api_search_progress(sid):
        d   = db()
        uid = session['uid']
        is_admin = session.get('role') == 'admin'
        row = d.execute(
            "SELECT initialized, init_rows_done, init_rows_total FROM searches WHERE id=?"
            + (" AND (user_id=? OR 1=?)" if not is_admin else ""),
            (sid, uid, 1) if not is_admin else (sid,)
        ).fetchone()
        if not row:
            return jsonify(error='not found'), 404
        done  = row['init_rows_done']  or 0
        total = row['init_rows_total'] or 0
        pct   = round(done / total * 100) if total > 0 else (100 if row['initialized'] else 0)
        return jsonify(
            initialized = bool(row['initialized']),
            done        = done,
            total       = total,
            pct         = pct,
        )

    # ══════════════════════════════════════════════════════════════
    # EXPORTAR A EXCEL
    # ══════════════════════════════════════════════════════════════
    @app.route('/export', methods=['POST'])
    @login_required
    def export():
        from alerts.excel_report import build_workbook

        search_ids = request.form.getlist('search_ids')
        search_ids = [int(x) for x in search_ids if x.isdigit()]
        if not search_ids:
            flash('Selecciona al menos una búsqueda.', 'warning')
            return redirect(url_for('dashboard'))
        # Filtros opcionales (solo aplican si viene de una búsqueda individual)
        _exp_kfs       = request.form.getlist('kw')
        _exp_cfs       = request.form.getlist('ch')
        _exp_pfs       = request.form.getlist('prog')
        _exp_mfs       = request.form.getlist('mt')
        _exp_date_from = request.form.get('date_from', '')
        _exp_date_to   = request.form.get('date_to', '')
        _exp_single    = len(search_ids) == 1   # filtros solo para exportación individual

        d   = db()
        tdb = _connect_trans_db()

        sheets_data = []
        for sid in search_ids:
            s = _get_search(sid)
            if not s:
                continue

            if _exp_single and any([_exp_kfs, _exp_cfs, _exp_pfs, _exp_mfs, _exp_date_from, _exp_date_to]):
                _w, _p = _match_where(sid, _exp_kfs, _exp_cfs, _exp_pfs, _exp_mfs, _exp_date_from, _exp_date_to)
                matches = d.execute(
                    f"SELECT * FROM matches WHERE {_w} ORDER BY timestamp ASC", _p
                ).fetchall()
            else:
                matches = d.execute(
                    "SELECT * FROM matches WHERE search_id=? ORDER BY timestamp ASC", (sid,)
                ).fetchall()
            sheets_data.append((s, matches))

        if not sheets_data:
            tdb.close()
            flash('No se encontraron búsquedas válidas.', 'warning')
            return redirect(url_for('dashboard'))

        output = build_workbook(sheets_data, d, tdb)
        tdb.close()

        filename = f"monitoreo_iteso_{date.today().isoformat()}.xlsx"
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=filename,
        )

    return app
