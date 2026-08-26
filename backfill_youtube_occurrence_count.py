"""
backfill_youtube_occurrence_count.py — Para las coincidencias de YouTube ya
existentes (creadas antes de que el watcher contara ocurrencias y filtrara
por idioma):
  1. Recalcula matches.occurrence_count re-consultando la transcripción de
     cada video distinto (una sola vez por video) y contando en cuántos
     segmentos de ~30s aparece la keyword, con el mismo criterio (fonético/
     palabra completa) que usó cada búsqueda.
  2. Si el video NO está en español/inglés, borra sus coincidencias --
     mismo filtro que ya aplica el watcher para videos nuevos.

Comitea después de CADA video (no al final) para no perder avance si se
interrumpe -- se puede volver a correr, los videos ya procesados en una
corrida anterior no tienen coincidencias con occurrence_count=1 REAL que
recontar dos veces (idempotente por diseño: siempre recalcula desde cero
para lo que sigue en la tabla).

Uso: venv/bin/python3 backfill_youtube_occurrence_count.py
"""
import json
import logging
import re
import sqlite3
import time
from pathlib import Path

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("backfill")

from alerts.watcher import _match
from alerts.youtube import get_transcript, detect_language

BASE_DIR = Path(__file__).parent
tmp_root = BASE_DIR / "tmp_youtube"

conn = sqlite3.connect(str(BASE_DIR / "alerts.db"))
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT m.id, m.search_id, m.keyword, m.channel_name, m.source_url, s.phonetic, s.whole_word
    FROM matches m JOIN searches s ON s.id = m.search_id
    WHERE m.channel_id = 9002
    ORDER BY m.source_url
""").fetchall()

by_video: dict[str, list] = {}
for r in rows:
    m = re.search(r'[?&]v=([\w-]{11})', r["source_url"] or "")
    if m:
        by_video.setdefault(m.group(1), []).append(r)

print(f"{len(rows)} coincidencias, {len(by_video)} videos distintos", flush=True)

updated = 0
deleted = 0
failed = 0

for i, (vid, video_rows) in enumerate(by_video.items(), 1):
    title = (video_rows[0]["channel_name"] or "")[:45]
    print(f"[{i}/{len(by_video)}] {vid} — {title}", flush=True)
    try:
        result = get_transcript(vid, tmp_root, logger)
    except Exception as e:
        print(f"  error consultando: {e}", flush=True)
        result = None

    if not result or not result["segments"]:
        failed += 1
        time.sleep(1.5)
        continue

    segments = result["segments"]
    sample = ' '.join(t for _, t in segments[:10]).strip()
    lang = detect_language(sample) if sample else None

    if lang is not None and lang not in {'es', 'en'}:
        ids = [r["id"] for r in video_rows]
        conn.executemany("DELETE FROM matches WHERE id=?", [(x,) for x in ids])
        conn.commit()
        deleted += len(ids)
        print(f"  idioma detectado: {lang} -- {len(ids)} coincidencia(s) borrada(s)", flush=True)
        time.sleep(1.5)
        continue

    for r in video_rows:
        count = sum(1 for _, text in segments
                    if _match(text, r["keyword"], bool(r["phonetic"]), bool(r["whole_word"])))
        count = max(count, 1)
        conn.execute("UPDATE matches SET occurrence_count=? WHERE id=?", (count, r["id"]))
        updated += 1
    conn.commit()
    time.sleep(1.5)  # cortesía con la API de YouTube -- evita el 429 visto en la corrida anterior

print(f"listo -- {updated} coincidencias actualizadas, {deleted} borradas por idioma, "
      f"{len(by_video)} videos distintos, {failed} sin transcripción disponible")
