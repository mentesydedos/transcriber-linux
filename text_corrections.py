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
