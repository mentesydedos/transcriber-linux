#!/usr/bin/env python3
"""
Endpoint HTTP de retrieval para el sistema de preguntas en lenguaje natural.

Corre en ESTA maquina (donde vive transcriptions.db). NO genera respuestas --
solo devuelve los fragmentos de transcripcion mas relevantes a una pregunta
(retrieval hibrido FTS5 + vectores, ver rag_search.py). La generacion del
lenguaje la hace la maquina de IA local (otra maquina en la LAN), que le
pega a este endpoint para obtener contexto real antes de responder.

Bind solo a la IP LAN (no 0.0.0.0 hacia toda interfaz) + token compartido
por header, ya que esto expone contenido de transcripciones a otra maquina
en la red.
"""
import os
from pathlib import Path

from flask import Flask, request, jsonify

import rag_search

# Carga el modelo de embeddings al importar el modulo (no en el primer
# request) -- con gunicorn --preload esto corre una sola vez en el proceso
# maestro antes de bifurcar los workers, asi ninguno paga el costo de carga
# en su primera consulta real.
rag_search._get_model()

TOKEN_FILE = Path(os.environ.get("RAG_API_TOKEN_FILE", "/home/transcriber/.rag-api-token"))
_TOKEN = TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else None

app = Flask(__name__)


def _check_auth():
    if not _TOKEN:
        return True  # sin token configurado -- solo para pruebas locales
    got = request.headers.get("X-RAG-Token", "")
    return got == _TOKEN


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/search", methods=["GET", "POST"])
def search():
    if not _check_auth():
        return jsonify({"error": "token invalido"}), 401

    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
    else:
        data = request.args

    q = (data.get("q") or "").strip()
    if not q:
        return jsonify({"error": "falta 'q' (la pregunta o consulta)"}), 400

    k = int(data.get("k", 15))
    channel_id = data.get("channel_id")
    channel_id = int(channel_id) if channel_id not in (None, "") else None
    channel_name = (data.get("channel_name") or "").strip() or None
    since = data.get("since") or None
    until = data.get("until") or None

    chunks = rag_search.search(q, k=k, channel_id=channel_id, channel_name=channel_name,
                                since=since, until=until)
    return jsonify({
        "query": q,
        "results": [
            {
                "id": c.id,
                "channel_id": c.channel_id,
                "channel_name": c.channel_name,
                "timestamp": c.timestamp,
                "text": c.text,
                "score": round(c.score, 4),
                "match_type": c.match_type,
            }
            for c in chunks
        ],
    })


if __name__ == "__main__":
    bind_host = os.environ.get("RAG_API_HOST", "148.201.38.17")
    bind_port = int(os.environ.get("RAG_API_PORT", "8765"))
    app.run(host=bind_host, port=bind_port)
