#!/usr/bin/env python3
"""
Indexado incremental de transcriptions.db hacia un indice de busqueda
(rag_index.db, archivo SEPARADO) para el sistema de preguntas en lenguaje
natural (RAG). Se mantiene en un archivo aparte a proposito -- nunca escribe
en transcriptions.db ni compite por sus locks/WAL con los motores de ASR que
lo escriben 24/7.

Dos marcas de agua (dos direcciones de progreso), guardadas en STATE_FILE
como "high,low":
  - high_watermark: todo id > high_watermark es NUEVO (llegado despues de la
    ultima corrida) -- se procesa siempre primero, sin importar que tan
    atrasado este el backfill historico. Las preguntas en /ask casi siempre
    son sobre lo reciente, no tiene sentido hacerlas esperar al historico.
  - low_watermark: todo id < low_watermark todavia no se indexo hacia atras
    en el tiempo -- se rellena progresivamente, mas lento, sin bloquear lo
    anterior.

Corre como systemd timer (rag-index.service/.timer) cada 5 min -- normalmente
rapido (solo lo nuevo). El backfill historico inicial (households de horas)
se corre una vez manualmente en segundo plano; el lock evita que el timer se
solape con esa corrida larga.
"""
import os

# Debe ir ANTES de importar torch/sentence-transformers (mas abajo, dentro de
# main()) -- si no, torch usa todos los nucleos por defecto (medido: 120
# hilos por proceso) y compite por CPU con la transcripcion en vivo. Este
# proceso hace embeddings de a un chunk/consulta corto, no necesita eso.
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")

import fcntl
import sqlite3
import sys
import time
from pathlib import Path

LOCK_FILE = Path(os.environ.get("RAG_INDEX_LOCK", "logs/rag_index.lock"))

TRANSCRIPTIONS_DB = os.environ.get("TRANSCRIBER_DB", "transcriptions.db")
RAG_DB            = os.environ.get("RAG_INDEX_DB", "rag_index.db")
STATE_FILE        = Path(os.environ.get("RAG_INDEX_STATE", "logs/rag_index_watermarks.txt"))
BATCH_SIZE        = int(os.environ.get("RAG_INDEX_BATCH", "256"))
EMBED_DIM         = 384  # intfloat/multilingual-e5-small

SILENCE_MARKERS = {"[~]", ""}


def _load_watermarks(max_id: int) -> tuple[int, int]:
    """Devuelve (high, low). Primera corrida: high=0 (todo es 'nuevo' una
    vez), low=max_id+1 (nada indexado hacia atras todavia)."""
    if STATE_FILE.exists():
        try:
            high, low = STATE_FILE.read_text().strip().split(",")
            return int(high), int(low)
        except Exception:
            pass
    return 0, max_id + 1


def _save_watermarks(high: int, low: int):
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(f"{high},{low}")
    tmp.rename(STATE_FILE)


def _ensure_schema(rag_con: sqlite3.Connection):
    rag_con.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            channel_name TEXT,
            timestamp TEXT NOT NULL,
            unix_ts REAL NOT NULL,
            text TEXT NOT NULL
        )
    """)
    rag_con.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            text, channel_name, content='chunks', content_rowid='id'
        )
    """)
    rag_con.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
            id INTEGER PRIMARY KEY,
            embedding FLOAT[{EMBED_DIM}]
        )
    """)
    rag_con.commit()


def _index_rows(rag_con, model, rows, on_batch_done):
    """Embebe e inserta rows (lista de tuplas de transcriptions) en lotes.
    on_batch_done(batch) se llama tras cada commit, para actualizar el
    watermark correspondiente con el progreso real."""
    n_done = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        texts = [f"passage: {r[5]}" for r in batch]
        embeddings = model.encode(texts, show_progress_bar=False, convert_to_numpy=True)

        for (cid, channel_id, channel_name, ts, unix_ts, text), emb in zip(batch, embeddings):
            rag_con.execute(
                "INSERT OR REPLACE INTO chunks (id, channel_id, channel_name, timestamp, unix_ts, text) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (cid, channel_id, channel_name, ts, unix_ts, text),
            )
            rag_con.execute(
                "INSERT OR REPLACE INTO chunks_fts (rowid, text, channel_name) VALUES (?, ?, ?)",
                (cid, text, channel_name),
            )
            rag_con.execute(
                "INSERT OR REPLACE INTO chunks_vec (id, embedding) VALUES (?, ?)",
                (cid, emb.astype("float32").tobytes()),
            )
        rag_con.commit()
        on_batch_done(batch)
        n_done += len(batch)
    return n_done


def main():
    import sqlite_vec
    import torch
    torch.set_num_threads(2)  # defensa adicional, ver nota de OMP/MKL arriba
    from sentence_transformers import SentenceTransformer

    # Evita que el timer periodico se solape con una corrida larga en curso
    # (p.ej. el backfill historico inicial, que puede tardar horas) -- dos
    # procesos escribiendo rag_index.db a la vez arriesgan "database is locked".
    LOCK_FILE.parent.mkdir(exist_ok=True)
    lock_fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Ya hay una corrida de rag_index.py en curso -- saliendo.")
        sys.exit(0)

    src = sqlite3.connect(TRANSCRIPTIONS_DB)
    src.execute("PRAGMA query_only = 1")  # nunca escribir en transcriptions.db
    max_id = src.execute("SELECT MAX(id) FROM transcriptions").fetchone()[0] or 0

    high, low = _load_watermarks(max_id)

    rag_con = sqlite3.connect(RAG_DB)
    rag_con.enable_load_extension(True)
    sqlite_vec.load(rag_con)
    rag_con.enable_load_extension(False)
    _ensure_schema(rag_con)

    model = None

    def get_model():
        nonlocal model
        if model is None:
            model = SentenceTransformer("intfloat/multilingual-e5-small")
        return model

    t0 = time.time()
    total_done = 0

    # 1) Adelante: todo lo NUEVO desde la ultima corrida (siempre primero --
    #    las preguntas en /ask son sobre lo reciente, esto nunca debe esperar
    #    al backfill historico).
    new_rows = src.execute("""
        SELECT id, channel_id, channel_name, timestamp, unix_ts, text
        FROM transcriptions WHERE id > ? ORDER BY id
    """, (high,)).fetchall()
    new_rows = [r for r in new_rows if (r[5] or "").strip() not in SILENCE_MARKERS]
    if new_rows:
        print(f"Adelante: {len(new_rows)} chunks nuevos (desde id={high})...")

        def _bump_high(batch):
            nonlocal high
            high = max(high, max(r[0] for r in batch))
            _save_watermarks(high, low)

        total_done += _index_rows(rag_con, get_model(), new_rows, _bump_high)
        high = max([high] + [r[0] for r in new_rows])
        _save_watermarks(high, low)

    # 2) Atras: rellena historico progresivamente, del mas reciente al mas
    #    viejo, mientras low_watermark > 1.
    if low > 1:
        old_rows = src.execute("""
            SELECT id, channel_id, channel_name, timestamp, unix_ts, text
            FROM transcriptions WHERE id < ? ORDER BY id DESC
        """, (low,)).fetchall()
        old_rows = [r for r in old_rows if (r[5] or "").strip() not in SILENCE_MARKERS]
        if old_rows:
            print(f"Atras (historico): {len(old_rows)} chunks pendientes (hasta id=1)...")

            def _bump_low(batch):
                nonlocal low
                low = min(low, min(r[0] for r in batch))
                _save_watermarks(high, low)

            total_done += _index_rows(rag_con, get_model(), old_rows, _bump_low)
            low = min([low] + [r[0] for r in old_rows])
            _save_watermarks(high, low)

    if total_done == 0:
        print("Sin filas nuevas para indexar (adelante y atras al dia).")
        return

    dt = time.time() - t0
    print(f"Listo: {total_done} chunks indexados en {dt:.1f}s ({total_done/dt:.1f} chunks/s). "
          f"watermarks high={high} low={low}")


if __name__ == "__main__":
    main()
