"""
text_corrections.py — Diccionario de corrección post-transcripción: pares
(patrón, reemplazo) aplicados al texto justo después de transcribir, antes
de guardarlo. Pensado para errores CONSISTENTES y conocidos del modelo
(nombres propios, siglas, nombres de estación) que el propio motor repite
siempre igual -- no es un mecanismo de aprendizaje, es una lista simple que
se corrige a mano conforme se detectan casos reales (ver text_corrections
en alerts.db, tabla editable).

Se usa sobre todo en transcriber_parakeet.py (radio): a diferencia de
transcriber_ctc_es.py (TV), Parakeet-TDT no tiene todavía un mecanismo de
refuerzo de vocabulario seguro para producción -- se probó (ver
proto_boosting/) y el rango de valores disponible no dio un punto estable
entre "sin efecto" y "alucina frases que no se dijeron", así que aquí la
corrección es puramente textual, sobre el resultado ya decodificado.

Cacheado en memoria con TTL corto (no requiere reiniciar el motor para que
una corrección nueva tome efecto, solo esperar unos segundos) -- mismo
patrón que otros caches del proyecto (ver _CHANNELS_CACHE_TTL en
alerts/transcript_archive.py).
"""
import re
import sqlite3
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
ALERTS_DB = BASE_DIR / "alerts.db"
CACHE_TTL_SEC = 60

_compiled = None
_compiled_at = 0.0


def _load_corrections() -> list[tuple[str, str]]:
    try:
        conn = sqlite3.connect(str(ALERTS_DB), timeout=5)
        rows = conn.execute("SELECT pattern, replacement FROM text_corrections").fetchall()
        conn.close()
        return list(rows)
    except Exception:
        return []


def _get_compiled():
    global _compiled, _compiled_at
    now = time.time()
    if _compiled is not None and now - _compiled_at < CACHE_TTL_SEC:
        return _compiled
    pairs = []
    for pattern, replacement in _load_corrections():
        try:
            # \b funciona bien con acentos/eñes en Python 3 (str = Unicode
            # por default) -- "raditeso" no coincide dentro de otra palabra
            # más larga por accidente.
            pairs.append((re.compile(r'\b' + re.escape(pattern) + r'\b', re.IGNORECASE), replacement))
        except re.error:
            continue
    _compiled = pairs
    _compiled_at = now
    return _compiled


def apply_corrections(text: str) -> str:
    if not text:
        return text
    for regex, replacement in _get_compiled():
        text = regex.sub(replacement, text)
    return text


TRANS_DB = BASE_DIR / "transcriptions.db"


def apply_to_history(pattern: str | None = None, replacement: str | None = None) -> dict:
    """Aplica retroactivamente la(s) corrección(es) a lo YA transcrito en
    transcriptions.db -- las correcciones nuevas solo aplican en vivo de
    aquí en adelante (ver apply_corrections); esto corrige también el
    historial existente. Si se pasan pattern/replacement, aplica solo esa
    (recién agregada); si no, reaplica TODAS las de la tabla.

    Usa transcriptions_fts (FTS5) para encontrar candidatos rápido en vez
    de escanear las 7M+ filas. FTS5 tokeniza letras+dígitos pegados como
    UN solo token (ej. "raditeson95" es un token, no "raditeson" + "95"),
    así que se busca por prefijo (ver _fts_prefix_term) -- coincidencia
    exacta se perdía casos como "Raditeson95.1" (medido: 1 fragmento real
    que exacta no encontraba y prefijo sí).

    Actualiza tanto transcriptions.text como transcriptions_fts (sin
    triggers automáticos en esta base -- se actualiza el índice a mano,
    mismo patrón que usa save_to_db() al insertar por primera vez).

    Devuelve {"total": n, "por_correccion": [(pattern, replacement, n), ...]}."""
    if pattern is not None and replacement is not None:
        corrections = [(pattern, replacement)]
    else:
        corrections = _load_corrections()

    conn = sqlite3.connect(str(TRANS_DB))
    conn.row_factory = sqlite3.Row
    total = 0
    detail = []
    for pat, repl in corrections:
        regex = re.compile(r'\b' + re.escape(pat) + r'\b', re.IGNORECASE)
        fts_term = _fts_prefix_term(pat)
        try:
            rowids = [r[0] for r in conn.execute(
                "SELECT rowid FROM transcriptions_fts WHERE transcriptions_fts MATCH ?", (fts_term + '*',)
            ).fetchall()]
        except sqlite3.OperationalError:
            detail.append((pat, repl, 0))
            continue

        changed = 0
        for rowid in rowids:
            row = conn.execute("SELECT id, channel_name, text FROM transcriptions WHERE id=?", (rowid,)).fetchone()
            if row is None:
                continue
            new_text = regex.sub(repl, row["text"])
            if new_text != row["text"]:
                conn.execute("UPDATE transcriptions SET text=? WHERE id=?", (new_text, row["id"]))
                conn.execute("DELETE FROM transcriptions_fts WHERE rowid=?", (row["id"],))
                conn.execute("INSERT INTO transcriptions_fts(rowid, text, channel_name) VALUES (?,?,?)",
                             (row["id"], new_text, row["channel_name"]))
                changed += 1
        conn.commit()
        total += changed
        detail.append((pat, repl, changed))
    conn.close()

    matches_fixed = _apply_to_matches(corrections)
    total += matches_fixed
    return {"total": total, "por_correccion": detail, "matches": matches_fixed}


def _apply_to_matches(corrections: list[tuple[str, str]]) -> int:
    """matches.matched_text (alerts.db) es una COPIA del texto tomada al
    momento en que se encontró la coincidencia -- corregir transcriptions
    no la toca. Sin esto, una coincidencia guardada ANTES de agregar la
    corrección seguía mostrando el error en los resultados de búsqueda
    aunque el archivo maestro (transcriptions.db) ya estuviera limpio
    (caso real: "Campus de Liteso" reportado 2026-09-07). La tabla es
    chica (miles de filas, no millones) -- no hace falta FTS aquí, un
    LIKE alcanza."""
    conn = sqlite3.connect(str(ALERTS_DB))
    conn.row_factory = sqlite3.Row
    changed = 0
    for pat, repl in corrections:
        regex = re.compile(r'\b' + re.escape(pat) + r'\b', re.IGNORECASE)
        like_term = f"%{_fts_prefix_term(pat)}%"
        rows = conn.execute(
            "SELECT id, matched_text FROM matches WHERE matched_text LIKE ? COLLATE NOCASE", (like_term,)
        ).fetchall()
        for row in rows:
            new_text = regex.sub(repl, row["matched_text"])
            if new_text != row["matched_text"]:
                conn.execute("UPDATE matches SET matched_text=? WHERE id=?", (new_text, row["id"]))
                changed += 1
    conn.commit()
    conn.close()
    return changed


def _fts_prefix_term(pattern: str) -> str:
    """Primer 'token' alfabético del patrón, sin dígitos/puntuación --
    FTS5 trata "." como carácter especial de sintaxis (ej. "raditeson95.1"
    truena con "syntax error near ."), y de todas formas un prefijo más
    corto sigue sirviendo como filtro amplio (la regex exacta decide qué
    fragmentos cambian de verdad)."""
    m = re.match(r'^[^\W\d_]+', pattern.split()[0])
    return m.group(0) if m else re.sub(r'[^\w]', '', pattern.split()[0])
