"""
alerts/similarity.py — Detección de fragmentos repetidos/similares dentro de
las coincidencias de una búsqueda (p.ej. el mismo comercial de radio/TV que
se repite varias veces al día, con pequeñas variaciones de transcripción por
ASR entre una pasada y otra).

Enfoque: cada coincidencia guarda una ventana de ±30 palabras alrededor de la
keyword. Comparar esas ventanas completas entre sí (por Jaccard de shingles,
el enfoque anterior) falla cuando la keyword aparece VARIAS veces dentro del
mismo spot -- p.ej. "Pantene" aparece 3 veces en un mismo comercial -- porque
según en qué mención se centró la ventana, el ruido alrededor (contenido NO
relacionado, antes o después del spot) diluye la similitud del conjunto
completo aunque las dos ventanas compartan 150+ caracteres idénticos del
mismo guion.

En vez de eso: se ubica dónde aparece la keyword en cada texto (puede tener
varias ocurrencias) y, para cada par candidato, se EXPANDE palabra por
palabra hacia la izquierda y la derecha desde esas anclas -- la "vecindad" de
la palabra encontrada -- contando cuánto coincide antes de la primera
diferencia real de contenido. Esto mide directamente cuánto texto comparten
alrededor de la mención, sin que el ruido del resto de la ventana lo diluya.
Para generar candidatos sin comparar todos los pares entre sí (una búsqueda
puede tener miles de coincidencias) se usa un índice invertido de shingles de
palabras: cualquier par que comparta al menos un shingle de 5 palabras es
candidato -- barato, y no se le escapan pares con similitud global baja pero
un tramo local largo compartido (a diferencia de MinHash/LSH, calibrado para
similitud de TODO el documento).

Los pares que empatan >=MIN_MATCH_WORDS se unen por transitividad (Union-Find
clásico). El umbral importa: con uno muy bajo (8 palabras probado), un
intermediario ambiguo puede empatar >=8 palabras con DOS comerciales
distintos de la misma campaña por separado (p.ej. dos anuncios de Pantene
que solo comparten la frase "Pantene Molecular Bone Repair", 4 palabras, más
relleno alrededor que por casualidad suma 8) y la transitividad los termina
mezclando en un solo cluster. Subir el umbral a 12 (validado contra datos
reales) elimina esas mezclas sin perder la cobertura del mismo comercial
repetido -- las alternativas más estrictas (exigir que cada miembro nuevo
empate directo contra UN representante fijo, o contra una muestra de los
miembros ya admitidos) sacrifican demasiado recall: fragmentan una misma
campaña en muchos clusters chicos porque el ruido de ASR hace que no todos
los pares directos alcancen el umbral, aunque transitivamente sí sea "la
misma cadena".

Es lo bastante rápido (segundos, no minutos) para correr sincrónico dentro
del request; el resultado se cachea en similarity_reports y se recalcula
solo si cambió el número de coincidencias de la búsqueda o si se pide
explícitamente (?refresh=1).
"""
import json
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path

BASE_DIR  = Path(__file__).parent.parent
ALERTS_DB = BASE_DIR / "alerts.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS similarity_reports (
    search_id    INTEGER PRIMARY KEY,
    match_count  INTEGER NOT NULL,
    result_json  TEXT    NOT NULL,
    generated_at TEXT    DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (search_id) REFERENCES searches(id)
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(ALERTS_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


# ── Normalización ────────────────────────────────────────────────────────────
SHINGLE_SIZE = 5  # ventana de palabras para el índice de candidatos


# Tabla de traducción para acentos comunes (español + préstamos frecuentes en
# transcripciones con code-switching a inglés/francés) -- mucho más rápida que
# unicodedata.normalize('NFKD', ...) por caracter, que domina el perfil con
# cientos de miles de llamadas (una por palabra) en búsquedas grandes. Un
# caracter acentuado que no esté en la tabla simplemente no se normaliza
# (sigue contando como alfanumérico), no se descarta ni rompe nada.
_ACCENT_SRC   = 'áéíóúüñàèìòùâêîôûäëïöç'
_ACCENT_DST   = 'aeiouunaeiouaeiouaeioc'
_ACCENT_TRANS = str.maketrans(_ACCENT_SRC, _ACCENT_DST)


def _normalize_word(w: str) -> str:
    """Una sola palabra a minúsculas/sin acentos/solo alfanumérico -- para
    comparar palabra a palabra (anclas y expansión), preservando la posición
    original de cada palabra (a diferencia de _normalize, que colapsa todo
    el texto en una sola cadena)."""
    base = w.lower().translate(_ACCENT_TRANS)
    return ''.join(c for c in base if c.isalnum())


def _content_words(text: str) -> tuple[list[str], list[str]]:
    """Separa text en palabras y descarta los tokens que no tengan ningún
    caracter alfanumérico (una "," suelta, puntos suspensivos solos, etc).
    Sin esto, dos transcripciones del MISMO audio que solo difieren en cómo
    quedó separada la puntuación ("Pantene No." vs "Pantene , no") terminan
    con distinta cantidad de "palabras" antes de ese punto -- todo lo que
    sigue queda desalineado índice a índice y la expansión desde el ancla se
    corta ahí aunque el contenido real siga siendo idéntico. Devuelve
    (palabras_originales, palabras_normalizadas) ya filtradas y alineadas
    entre sí (mismo índice = mismo token)."""
    orig, norm = [], []
    for w in text.split():
        n = _normalize_word(w)
        if n:
            orig.append(w)
            norm.append(n)
    return orig, norm


def _shingles(norm_text: str) -> set[str]:
    words = norm_text.split()
    if len(words) < SHINGLE_SIZE:
        return {norm_text} if norm_text else set()
    return {' '.join(words[i:i + SHINGLE_SIZE]) for i in range(len(words) - SHINGLE_SIZE + 1)}


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Como _normalize, pero además devuelve un mapa índice-a-índice hacia el
    texto ORIGINAL -- permite comparar de forma tolerante a mayúsculas/acentos/
    puntuación y aun así recortar el resultado del texto real (con su
    capitalización tal cual se transcribió). Usado por alerts/app.py
    (_mark_fragment) para resaltar el fragmento común dentro de cada
    aparición individual -- se llama por cada ocurrencia mostrada, así que
    usa la tabla de traducción (no unicodedata.normalize por caracter, que
    con miles de llamadas dominaba el tiempo de la página)."""
    out_chars, out_map = [], []
    prev_space = True
    for i, ch in enumerate(text):
        base = ch.lower().translate(_ACCENT_TRANS)
        if base.isalnum():
            out_chars.append(base)
            out_map.append(i)
            prev_space = False
        elif not prev_space:
            out_chars.append(' ')
            out_map.append(i)
            prev_space = True
    while out_chars and out_chars[-1] == ' ':
        out_chars.pop()
        out_map.pop()
    return ''.join(out_chars), out_map


# ── Anclaje en la keyword + expansión a su vecindad ─────────────────────────
def _keyword_positions(norm_words: list[str], keyword: str) -> list[int]:
    """Todas las posiciones (índice de palabra) donde empieza la keyword
    (puede ser una frase de varias palabras) dentro de norm_words -- la misma
    keyword puede aparecer más de una vez en la ventana capturada (p.ej.
    "Pantene" 3 veces en un mismo comercial), y cada aparición es un ancla
    candidata distinta."""
    kw_norm = [_normalize_word(w) for w in keyword.split()]
    k = len(kw_norm)
    n = len(norm_words)
    if k == 0 or k > n:
        return []
    return [i for i in range(n - k + 1)
            if all(kw_norm[j] and kw_norm[j] in norm_words[i + j] for j in range(k))]


def _expand_from_anchor(norm_a: list[str], ia: int, norm_b: list[str], ib: int) -> tuple[int, int]:
    """Cuenta cuántas palabras coinciden yendo hacia la izquierda y hacia la
    derecha desde el ancla (ia en A, ib en B) -- la vecindad de la palabra
    encontrada. Se detiene en la primera palabra que no coincide: a esta
    altura (palabra por palabra, ya normalizada) una diferencia real es
    contenido distinto, no un acento/mayúscula/coma que ya se neutralizó."""
    left = 0
    i, j = ia - 1, ib - 1
    while i >= 0 and j >= 0 and norm_a[i] and norm_a[i] == norm_b[j]:
        left += 1
        i -= 1
        j -= 1
    right = 0
    i, j = ia + 1, ib + 1
    while i < len(norm_a) and j < len(norm_b) and norm_a[i] and norm_a[i] == norm_b[j]:
        right += 1
        i += 1
        j += 1
    return left, right


def _best_match(norm_a: list[str], anchors_a: list[int],
                 norm_b: list[str], anchors_b: list[int]) -> tuple[int, int, int] | None:
    """Prueba todas las combinaciones de anclas (una keyword puede tener
    varias ocurrencias en cada texto) y se queda con la vecindad más larga.
    Devuelve (total_palabras, rango_izq_en_A, rango_der_en_A) del mejor match,
    o None si no hay anclas de un lado o del otro."""
    best = None
    for ia in anchors_a:
        for ib in anchors_b:
            left, right = _expand_from_anchor(norm_a, ia, norm_b, ib)
            total = left + right + 1
            if best is None or total > best[0]:
                best = (total, ia - left, ia + right)
    return best


MIN_MATCH_WORDS  = 12   # mínimo de palabras consecutivas coincidentes para considerarlo "el mismo spot" --
                         # con 8 palabras, intermediarios ambiguos podían empatar >=8 con DOS anuncios
                         # distintos de la misma campaña por separado (p.ej. dos comerciales de Pantene
                         # que solo comparten "Pantene Molecular Bone Repair", 4 palabras) y el
                         # encadenamiento transitivo los terminaba mezclando en un solo cluster. Con 12
                         # ya no se observan mezclas así (validado contra datos reales) y sigue
                         # conectando bien las apariciones genuinas del mismo comercial (42/45 en un
                         # cluster, vs 44/45 con contaminación en 10, o clusters mucho más chicos con
                         # esquemas que exigen acuerdo directo contra cada miembro del grupo).
MAX_SHINGLE_DOCS = 150   # shingle compartido por demasiados documentos -> demasiado genérico, no sirve de candidato
MIN_CLUSTER      = 2


# ── Union-Find ───────────────────────────────────────────────────────────────
class _DSU:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


CLUSTER_WINDOW_WORDS = 50  # cuánto de cada lado del ancla se analiza para similitud --
                           # igual al ±50 palabras que se muestra en pantalla (alerts/app.py
                           # _center_text), para que el fragmento resultante nunca sea más
                           # largo que lo que realmente se alcanza a ver/resaltar.
                           # (no afecta lo que se guarda ni lo que se muestra al usuario
                           # -- matched_text puede traer hasta 3 chunks pegados para dar
                           # contexto visual/de timestamp, pero para detectar el mismo
                           # spot repetido con esto de sobra alcanza, y evita que el
                           # análisis se vuelva lento procesando texto irrelevante lejos
                           # de la mención real)
FRAGMENT_CAP_WORDS = 26    # tope al fragmento MOSTRADO cuando cruzó a otro anuncio -- ver nota en _fragment_for


def _cluster(rows: list[sqlite3.Row]) -> list[dict]:
    texts    = [r['matched_text'] or '' for r in rows]
    keywords = [r['keyword'] or '' for r in rows]

    words_list, norm_list, anchors_list = [], [], []
    for t, kw in zip(texts, keywords):
        ow, nw = _content_words(t)
        pos = _keyword_positions(nw, kw)
        anchor = pos[0] if pos else (len(nw) // 2 if nw else 0)
        wstart = max(0, anchor - CLUSTER_WINDOW_WORDS)
        wend   = min(len(nw), anchor + CLUSTER_WINDOW_WORDS + 1)
        words_list.append(ow[wstart:wend])
        norm_list.append(nw[wstart:wend])
        local_anchors = [p - wstart for p in pos if wstart <= p < wend]
        anchors_list.append(local_anchors or [anchor - wstart])

    # Índice invertido de shingles sobre la ventana (no el texto completo) --
    # solo para generar candidatos baratos (cualquier par que comparta un
    # shingle de 5 palabras). La decisión real de agrupar o no se toma
    # después, expandiendo desde el ancla real de la keyword en cada uno.
    norms = [' '.join(nw) for nw in norm_list]
    shs   = [_shingles(n) for n in norms]
    inverted: dict[str, list[int]] = {}
    for i, sset in enumerate(shs):
        for sh in sset:
            inverted.setdefault(sh, []).append(i)

    dsu = _DSU(len(rows))
    checked: set[tuple[int, int]] = set()
    for docs in inverted.values():
        if len(docs) < 2 or len(docs) > MAX_SHINGLE_DOCS:
            continue
        for x in range(len(docs)):
            for y in range(x + 1, len(docs)):
                a, b = docs[x], docs[y]
                pair = (a, b) if a < b else (b, a)
                if pair in checked:
                    continue
                checked.add(pair)
                best = _best_match(norm_list[a], anchors_list[a], norm_list[b], anchors_list[b])
                if best and best[0] >= MIN_MATCH_WORDS:
                    dsu.union(a, b)

    groups: dict[int, list[int]] = {}
    for i in range(len(rows)):
        groups.setdefault(dsu.find(i), []).append(i)
    raw_groups = [m for m in groups.values() if len(m) >= MIN_CLUSTER]

    def _fragment_for(members: list[int]) -> str:
        ref_i = max(members, key=lambda i: len(texts[i]))
        best_overall = None
        for i in members:
            if i == ref_i:
                continue
            m = _best_match(norm_list[ref_i], anchors_list[ref_i], norm_list[i], anchors_list[i])
            if m and (best_overall is None or m[0] > best_overall[0]):
                best_overall = m
        if not best_overall or best_overall[0] < MIN_MATCH_WORDS:
            return ''
        _, wstart, wend = best_overall
        span = wend - wstart + 1
        # Si el tramo encontrado ocupa CASI toda la ventana (±CLUSTER_WINDOW_WORDS
        # de cada lado del ancla), ya no es "el mismo anuncio" -- es el mismo
        # BLOQUE de comerciales completo repetido dos veces (p.ej. el mismo
        # anuncio siempre sale después de un promo de noticias y antes de otro
        # anuncio distinto, y ese bloque volvió a salir igual en otra emisión).
        # Ahí sí conviene recortar agresivo, centrado en la mención real de la
        # keyword. Un tramo simplemente largo pero que NO llegó al borde de la
        # ventana (un anuncio con guion largo y limpio) se deja tal cual --
        # cortarlo igual sería tirar contenido real sin necesidad.
        if span >= 0.9 * (2 * CLUSTER_WINDOW_WORDS + 1):
            center = next((a for a in anchors_list[ref_i] if wstart <= a <= wend),
                           (wstart + wend) // 2)
            half   = FRAGMENT_CAP_WORDS // 2
            wstart = max(wstart, center - half)
            wend   = min(wend, wstart + FRAGMENT_CAP_WORDS - 1)
        return ' '.join(words_list[ref_i][wstart:wend + 1]).strip()

    raw_fragments = [_fragment_for(m) for m in raw_groups]

    # Segunda pasada: fusionar clusters cuyo FRAGMENTO resulta prácticamente
    # el mismo texto -- pasa cuando el mismo anuncio se emite en radio Y TV
    # (voces/audio distintos, ASR distinto entre medios): ningún par
    # individual radio↔TV llega a MIN_MATCH_WORDS, pero cada cluster por su
    # cuenta ya convergió casi al mismo guion. Con cientos de clusters
    # comparar todos los fragmentos entre sí (O(n²)) ya no es barato -- se
    # genera candidatos igual que arriba, con un índice invertido de
    # shingles de 5 palabras sobre cada fragmento.
    frag_words = [[_normalize_word(w) for w in f.split()] for f in raw_fragments]
    frag_shingle_idx: dict[tuple, list[int]] = {}
    for i, fw in enumerate(frag_words):
        if len(fw) < SHINGLE_SIZE:
            continue
        for k in range(len(fw) - SHINGLE_SIZE + 1):
            frag_shingle_idx.setdefault(tuple(fw[k:k + SHINGLE_SIZE]), []).append(i)

    dsu2 = _DSU(len(raw_groups))
    checked2: set[tuple[int, int]] = set()
    for docs in frag_shingle_idx.values():
        if len(docs) < 2:
            continue
        for x in range(len(docs)):
            for y in range(x + 1, len(docs)):
                i, j = docs[x], docs[y]
                pair = (i, j) if i < j else (j, i)
                if pair in checked2:
                    continue
                checked2.add(pair)
                sm = SequenceMatcher(None, frag_words[i], frag_words[j], autojunk=False)
                m = sm.find_longest_match(0, len(frag_words[i]), 0, len(frag_words[j]))
                if m.size >= MIN_MATCH_WORDS:
                    dsu2.union(i, j)

    merged: dict[int, list[int]] = {}
    for i in range(len(raw_groups)):
        merged.setdefault(dsu2.find(i), []).extend(raw_groups[i])

    clusters = []
    for members in merged.values():
        members = sorted(members, key=lambda i: rows[i]['timestamp'] or '')
        fragment = _fragment_for(members)

        ch_counts: dict[str, int] = {}
        for i in members:
            ch = rows[i]['channel_name'] or '?'
            ch_counts[ch] = ch_counts.get(ch, 0) + 1
        channels = sorted(({'name': k, 'count': v} for k, v in ch_counts.items()),
                           key=lambda c: -c['count'])

        clusters.append({
            'size': len(members),
            'common_fragment': fragment,
            'channels': channels,
            'first_ts': rows[members[0]]['timestamp'],
            'last_ts': rows[members[-1]]['timestamp'],
            'occurrences': [{
                'id': rows[i]['id'],
                'channel_name': rows[i]['channel_name'],
                'keyword': rows[i]['keyword'],
                'timestamp': rows[i]['timestamp'],
                'matched_text': texts[i],
            } for i in members],
        })

    clusters.sort(key=lambda c: -c['size'])
    return clusters


# ── API pública ──────────────────────────────────────────────────────────────
def analyze(search_id: int) -> dict:
    conn = _conn()
    rows = conn.execute(
        "SELECT id, keyword, channel_name, timestamp, matched_text "
        "FROM matches WHERE search_id=? ORDER BY timestamp", (search_id,)
    ).fetchall()
    conn.close()

    clusters = _cluster(rows) if rows else []
    clustered_ids = {o['id'] for c in clusters for o in c['occurrences']}
    unmatched = sorted(
        (r for r in rows if r['id'] not in clustered_ids),
        key=lambda r: r['timestamp'] or '', reverse=True
    )
    unique_matches = [{
        'id': r['id'],
        'channel_name': r['channel_name'],
        'keyword': r['keyword'],
        'timestamp': r['timestamp'],
        'matched_text': r['matched_text'] or '',
    } for r in unmatched]

    return {
        'search_id': search_id,
        'match_count': len(rows),
        'clusters': clusters,
        'clustered_count': len(clustered_ids),
        'unique_count': len(unique_matches),
        'unique_matches': unique_matches,
    }


def _save(search_id: int, result: dict) -> None:
    conn = _conn()
    conn.execute("""
        INSERT INTO similarity_reports (search_id, match_count, result_json, generated_at)
        VALUES (?, ?, ?, datetime('now','localtime'))
        ON CONFLICT(search_id) DO UPDATE SET
            match_count=excluded.match_count,
            result_json=excluded.result_json,
            generated_at=excluded.generated_at
    """, (search_id, result['match_count'], json.dumps(result, ensure_ascii=False)))
    conn.commit()
    conn.close()


def _load_cached(search_id: int) -> tuple[dict, str] | tuple[None, None]:
    conn = _conn()
    row = conn.execute(
        "SELECT match_count, result_json, generated_at FROM similarity_reports WHERE search_id=?",
        (search_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None, None
    return json.loads(row['result_json']), row['generated_at']


def get_or_generate(search_id: int, force: bool = False) -> dict:
    """Reusa el análisis cacheado si el número de coincidencias no cambió
    desde la última corrida; si no, (re)calcula y guarda."""
    cached, generated_at = (None, None) if force else _load_cached(search_id)
    if cached is not None:
        conn = _conn()
        current_n = conn.execute(
            "SELECT COUNT(*) FROM matches WHERE search_id=?", (search_id,)
        ).fetchone()[0]
        conn.close()
        if current_n == cached['match_count']:
            cached['generated_at'] = generated_at
            cached['stale'] = False
            return cached
        cached['stale'] = True
        cached['current_match_count'] = current_n
        # Sigue mostrando lo cacheado (con aviso) -- quien quiera lo fresco usa ?refresh=1
        return cached

    result = analyze(search_id)
    _save(search_id, result)
    _, generated_at = _load_cached(search_id)
    result['generated_at'] = generated_at
    result['stale'] = False
    return result
