#!/usr/bin/env python3
"""
Retrieval hibrido (FTS5 + vectores) sobre rag_index.db para el sistema de
preguntas en lenguaje natural. NO genera respuestas -- solo devuelve los
fragmentos de transcripcion mas relevantes a una pregunta. La generacion del
lenguaje corre en otra maquina, que llama a esto via el endpoint HTTP
(ver rag_api.py).

rag_index.db es un archivo separado de transcriptions.db a proposito -- ver
rag_index.py.
"""
import os

# Debe ir ANTES de importar torch/sentence-transformers -- si no, torch usa
# todos los nucleos por defecto (medido: 120 hilos por proceso de gunicorn)
# y compite por CPU con la transcripcion en vivo. Aca solo se embeben
# consultas cortas de a una, no hace falta.
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")

import re
import sqlite3
from dataclasses import dataclass, asdict

RAG_DB = os.environ.get("RAG_INDEX_DB", "rag_index.db")
EMBED_MODEL_NAME = "intfloat/multilingual-e5-small"

_model = None


def _get_model():
    global _model
    if _model is None:
        import torch
        torch.set_num_threads(2)  # defensa adicional, ver nota de OMP/MKL arriba
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(EMBED_MODEL_NAME)
    return _model


def _connect():
    import sqlite_vec
    con = sqlite3.connect(RAG_DB)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    return con


@dataclass
class Chunk:
    id: int
    channel_id: int
    channel_name: str
    timestamp: str
    text: str
    score: float
    match_type: str  # "keyword", "semantic", o "keyword+semantic"


def search(query: str, k: int = 15, channel_id: int = None,
           channel_name: str = None,
           since: str = None, until: str = None) -> list[Chunk]:
    """Busqueda hibrida: top-k por FTS5 (palabra clave) unido con top-k por
    similitud vectorial (semantica), fusionados por id de chunk."""
    con = _connect()

    filters = []
    params_extra = []
    if channel_id is not None:
        filters.append("c.channel_id = ?")
        params_extra.append(channel_id)
    if channel_name:
        filters.append("c.channel_name = ?")
        params_extra.append(channel_name)
    if since:
        filters.append("c.timestamp >= ?")
        params_extra.append(since)
    if until:
        filters.append("c.timestamp <= ?")
        params_extra.append(until)
    where_extra = (" AND " + " AND ".join(filters)) if filters else ""

    results: dict[int, Chunk] = {}

    # 1) Keyword (FTS5) -- bm25, sirve para nombres propios, siglas, cifras.
    # Cada termino entre comillas dobles (frase literal de 1 palabra) -- FTS5
    # trata "?"/"?"/etc como sintaxis de operador si van sueltos y tira
    # "syntax error"; entre comillas se toman como texto literal.
    try:
        terms = [w for w in re.findall(r"\w+", query, flags=re.UNICODE) if len(w) > 2]
        fts_query = " OR ".join(f'"{t}"' for t in terms) or f'"{query}"'
        rows = con.execute(f"""
            SELECT c.id, c.channel_id, c.channel_name, c.timestamp, c.text,
                   bm25(chunks_fts) AS rank
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            WHERE chunks_fts MATCH ? {where_extra}
            ORDER BY rank LIMIT ?
        """, (fts_query, *params_extra, k)).fetchall()
        for cid, chid, cname, ts, text, rank in rows:
            results[cid] = Chunk(cid, chid, cname, ts, text, score=-rank, match_type="keyword")
    except sqlite3.OperationalError:
        pass  # query FTS invalida igual -- se sigue solo con semantico

    # 2) Semantico (vectores) -- sirve para parafraseo, sinonimos, sin coincidir palabra exacta.
    # OJO: sqlite-vec calcula el top-k MAS CERCANO GLOBAL antes de aplicar
    # cualquier filtro (fecha/canal) en el JOIN -- con pocos filtros activos,
    # los k vecinos mas cercanos globales pueden ser todos de otra fecha/canal
    # y el filtro los descarta a todos, devolviendo 0 aunque SI exista
    # contenido relevante dentro del filtro (probado: paso con "since" de
    # ultimas 24h). Corregido pidiendo de mas (over-fetch) cuando hay
    # filtros, y recortando a k despues de filtrar.
    model = _get_model()
    qvec = model.encode([f"query: {query}"], convert_to_numpy=True)[0].astype("float32").tobytes()
    vec_k = k * 30 if filters else k
    vec_k = min(vec_k, 2000)
    rows = con.execute(f"""
        SELECT c.id, c.channel_id, c.channel_name, c.timestamp, c.text, v.distance
        FROM chunks_vec v
        JOIN chunks c ON c.id = v.id
        WHERE v.embedding MATCH ? AND k = ? {where_extra}
        ORDER BY v.distance LIMIT ?
    """, (qvec, vec_k, *params_extra, k)).fetchall()
    for cid, chid, cname, ts, text, dist in rows:
        sim = 1 - dist  # cosine distance -> similitud aprox
        if cid in results:
            results[cid].match_type = "keyword+semantic"
            results[cid].score = max(results[cid].score, sim)
        else:
            results[cid] = Chunk(cid, chid, cname, ts, text, score=sim, match_type="semantic")

    con.close()
    ranked = sorted(results.values(), key=lambda c: c.score, reverse=True)
    return ranked[:k]


if __name__ == "__main__":
    import sys, json
    q = " ".join(sys.argv[1:]) or "temperatura en la zona metropolitana"
    for c in search(q, k=10):
        print(f"[{c.match_type:16s} {c.score:6.3f}] ({c.timestamp}) {c.channel_name}: {c.text[:120]}")
