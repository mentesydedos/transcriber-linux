"""
backfill_text_corrections.py — Aplica retroactivamente el diccionario de
corrección (text_corrections.py, tabla en alerts.db) a lo YA transcrito en
transcriptions.db -- las correcciones nuevas solo aplican de aquí en
adelante en vivo; esto es para corregir también el historial existente,
a pedido explícito (para poder validar visualmente que una corrección
recién agregada sí funciona).

Usa transcriptions_fts (FTS5) para encontrar candidatos rápido en vez de
escanear las 7M+ filas -- MATCH con el patrón (sin acentos/mayúsculas
específicas, FTS5 ya tokeniza así por default) trae solo las filas que
podrían tener esa palabra, luego se aplica la regex exacta (word-boundary,
case-insensitive) de siempre para decidir si de verdad cambia algo.

Actualiza tanto transcriptions.text como transcriptions_fts (sin triggers
automáticos en esta base -- se actualiza el índice a mano, mismo patrón que
usa save_to_db() al insertar por primera vez).

Uso: venv/bin/python3 backfill_text_corrections.py
"""
import re
import sqlite3

from text_corrections import _load_corrections

conn = sqlite3.connect("transcriptions.db")
conn.row_factory = sqlite3.Row

corrections = _load_corrections()
print(f"{len(corrections)} correcciones activas\n")

total_updated = 0
for pattern, replacement in corrections:
    regex = re.compile(r'\b' + re.escape(pattern) + r'\b', re.IGNORECASE)
    # FTS5 tokeniza por palabras -- basta la primera palabra del patrón
    # como término de búsqueda para traer TODOS los candidatos posibles
    # (la regex exacta decide después si de verdad aplica); patrones de
    # una sola palabra (el caso más común aquí) son el término completo.
    # Se recorta a solo el prefijo alfabético (sin dígitos/puntuación) --
    # FTS5 trata "." como carácter especial de sintaxis (ej. "raditeson95.1"
    # tronaba con "syntax error near ."), y de todas formas un término más
    # corto sigue sirviendo como filtro (la regex exacta decide después).
    fts_term = re.match(r'^[^\W\d_]+', pattern.split()[0])
    fts_term = fts_term.group(0) if fts_term else pattern.split()[0]
    try:
        rowids = [r[0] for r in conn.execute(
            "SELECT rowid FROM transcriptions_fts WHERE transcriptions_fts MATCH ?", (fts_term,)
        ).fetchall()]
    except sqlite3.OperationalError as e:
        print(f"  '{pattern}': término de búsqueda inválido para FTS ({e}), se omite")
        continue

    changed = 0
    examples = []
    for rowid in rowids:
        row = conn.execute("SELECT id, channel_name, text FROM transcriptions WHERE id=?", (rowid,)).fetchone()
        if row is None:
            continue
        new_text = regex.sub(replacement, row["text"])
        if new_text != row["text"]:
            conn.execute("UPDATE transcriptions SET text=? WHERE id=?", (new_text, row["id"]))
            conn.execute("DELETE FROM transcriptions_fts WHERE rowid=?", (row["id"],))
            conn.execute("INSERT INTO transcriptions_fts(rowid, text, channel_name) VALUES (?,?,?)",
                         (row["id"], new_text, row["channel_name"]))
            changed += 1
            if len(examples) < 2:
                examples.append((row["channel_name"], row["text"][:90], new_text[:90]))

    conn.commit()
    total_updated += changed
    print(f"'{pattern}' -> '{replacement}': {changed} fragmento(s) corregido(s)")
    for chname, before, after in examples:
        print(f"    [{chname}] antes: {before}")
        print(f"    [{chname}] ahora: {after}")

print(f"\nlisto -- {total_updated} fragmentos corregidos en total")
