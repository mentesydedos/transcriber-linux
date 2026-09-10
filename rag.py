"""
rag.py — Pipeline RAG (búsqueda FTS5 + LLM local) usado por alerts/app.py.

Exporta:
  extract_keywords(question)      → query string para FTS5
  build_context(rows)             → texto formateado como contexto del prompt
  rango_to_cutoff(rango)          → timestamp ISO para filtrar búsqueda
  ask_stream(question, ...)       → generator que emite dicts:
      {"type":"sources","items":[...]}
      {"type":"token","text":"…"}
      {"type":"done","elapsed":s}
      {"type":"error","message":"…"}
  RANGOS, SYSTEM_PROMPT
"""
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

import requests

LLM_MODEL    = "models/llm/Qwen2.5-3B-Instruct-Q4_K_M.gguf"
LLM_THREADS  = 8
LLM_CTX      = 4096
LLM_MAX_TOK  = 512

# Retrieval: rag-api.service (busqueda hibrida FTS5+vectores sobre
# rag_index.db, ver rag_search.py/rag_api.py) -- reemplaza al FTS5 puro de
# search.py (2026-08-11). Corre siempre en esta misma maquina, ya caliente
# (~30-50ms por consulta) -- llamada HTTP en vez de import directo porque
# rag_search.py vive en venv-rag (sentence-transformers/torch), un venv
# separado del que usa este proceso (alerts.service corre bajo venv/).
RAG_API_URL   = os.environ.get("RAG_API_URL", "http://148.201.38.17:8765/search")
_token_file   = Path(os.environ.get("RAG_API_TOKEN_FILE", "/home/transcriber/.rag-api-token"))
RAG_API_TOKEN = _token_file.read_text().strip() if _token_file.exists() else None


def search(query: str, canal: str = None, desde: str = None, hasta: str = None,
           limite: int = 20, contexto: bool = False):
    """Retrocompatible con la firma de search.py -- ahora llama a rag-api.service."""
    headers = {"X-RAG-Token": RAG_API_TOKEN} if RAG_API_TOKEN else {}
    resp = requests.post(RAG_API_URL, headers=headers, timeout=10, json={
        "q": query, "k": limite, "channel_name": canal, "since": desde, "until": hasta,
    })
    resp.raise_for_status()
    return resp.json()["results"]

_llm      = None
_llm_lock = threading.Lock()

def get_llm():
    """Carga el LLM la primera vez que se usa (lazy)."""
    global _llm
    with _llm_lock:
        if _llm is None:
            from llama_cpp import Llama
            _llm = Llama(
                model_path=LLM_MODEL,
                n_ctx=LLM_CTX,
                n_threads=LLM_THREADS,
                verbose=False,
            )
    return _llm


# ── Keyword extraction para FTS ──────────────────────────────────────────────
_STOPWORDS = set("""
a al algo alguna algunas alguno algunos ante antes aquel aquella aquellas aquello
aquellos aquí ayer bajo bien cada como con contra cual cuales cuando cuanta
cuantas cuanto cuantos cómo cuál cuándo cuánta cuántas cuánto cuántos de del desde
donde dónde dos el ella ellas ellos en entonces entre era eran eres es esa esas ese
eso esos esta estaba estado estamos están estar estas este esto estos fin fue fueron
ha hace hacen hasta hay he hemos hicieron hizo hoy la las le les lo los luego mas me
mi mis mucho muchos muy más ni no nos nosotros nuestra nuestras nuestro nuestros o
os otra otras otro otros para pero poco por porque pronto puede pues que qué quien
quienes quién quiénes se ser si sido siempre sobre solo somos son soy su sus sí sólo
también tampoco tan te tenemos tener tengo ti tiene tienen toda todas todo todos tras
tu tus tú un una unas uno unos usted ustedes va vaya vamos ven vez voy y ya yo él
""".split())

def extract_keywords(question: str, max_terms: int = 6) -> str:
    tokens = re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñü0-9]+", question)
    keep = []
    for t in tokens:
        tl = t.lower()
        if len(tl) <= 2:       continue
        if tl in _STOPWORDS:   continue
        keep.append(t)
        if len(keep) >= max_terms: break
    if not keep:
        return question
    return " OR ".join(f"{t}*" for t in keep)


# ── Rangos de tiempo ─────────────────────────────────────────────────────────
RANGOS = [
    ("all", "Todo el tiempo"),
    ("1h",  "Última hora"),
    ("6h",  "Últimas 6 horas"),
    ("24h", "Últimas 24 horas"),
    ("7d",  "Últimos 7 días"),
    ("30d", "Últimos 30 días"),
]

def rango_to_cutoff(rango: str):
    now = datetime.now()
    if rango == "1h":  return (now - timedelta(hours=1)).isoformat(sep=" ", timespec="seconds")
    if rango == "6h":  return (now - timedelta(hours=6)).isoformat(sep=" ", timespec="seconds")
    if rango == "24h": return (now - timedelta(days=1)).isoformat(sep=" ", timespec="seconds")
    if rango == "7d":  return (now - timedelta(days=7)).isoformat(sep=" ", timespec="seconds")
    if rango == "30d": return (now - timedelta(days=30)).isoformat(sep=" ", timespec="seconds")
    return None


# ── Prompt + pipeline ────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Eres un asistente que responde preguntas sobre transcripciones de canales de TV mexicana en español.

Reglas:
- Usa SOLO la información de los fragmentos del CONTEXTO. No inventes.
- Si la respuesta no está en el contexto, di exactamente: "No se menciona en las transcripciones disponibles."
- Sé directo y conciso (máximo 4-5 frases salvo que se pida más detalle).
- Al citar, usa el formato: [Nombre del canal, HH:MM] entre corchetes, al final de la frase relevante.
- Responde en español."""

def build_context(rows):
    lines = []
    for i, r in enumerate(rows, 1):
        ts = r["timestamp"] or ""
        ts_short = ts[11:19] if len(ts) >= 19 else ts
        lines.append(f"[{i}] [{r['channel_name']}, {ts_short}] {r['text']}")
    return "\n".join(lines)


def ask_stream(question: str, rango: str = "24h",
               canal: str = None, top_n: int = 15):
    """
    Pipeline RAG. Generator que yields dicts JSON-serializables.
    El caller los convierte a NDJSON para el navegador.
    """
    if not question or not question.strip():
        yield {"type": "error", "message": "pregunta vacía"}
        return

    top_n = max(3, min(int(top_n or 15), 30))
    desde = rango_to_cutoff(rango)

    # Pregunta completa en lenguaje natural, sin extraer keywords -- rag-api
    # ya hace su propia busqueda hibrida (FTS5 + embeddings semanticos), y el
    # texto natural rinde mejor para la parte semantica que un query tipo
    # "termino1* OR termino2*" (eso era necesario solo para el FTS5 puro de
    # antes, ver extract_keywords -- se deja la funcion por si se usa en otro lado).
    try:
        rows = search(query=question, canal=canal or None,
                      desde=desde, hasta=None, limite=top_n)
    except Exception as e:
        yield {"type": "error", "message": f"retrieval: {e}"}
        return

    src_items = [{"channel_name": r["channel_name"],
                  "timestamp":    r["timestamp"],
                  "text":         r["text"]}
                 for r in rows]
    yield {"type": "sources", "items": src_items}

    try:
        llm = get_llm()
    except Exception as e:
        yield {"type": "error", "message": f"LLM load: {e}"}
        return

    context     = build_context(rows) if rows else "(sin fragmentos relevantes)"
    user_prompt = f"CONTEXTO:\n{context}\n\nPREGUNTA: {question}\n\nRESPUESTA:"

    t0 = datetime.now()
    try:
        stream = llm.create_chat_completion(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
            max_tokens=LLM_MAX_TOK,
            temperature=0.2,
            stream=True,
        )
        for chunk in stream:
            delta = chunk["choices"][0].get("delta", {})
            tok = delta.get("content")
            if tok:
                yield {"type": "token", "text": tok}
    except Exception as e:
        yield {"type": "error", "message": f"LLM: {e}"}
        return

    yield {"type": "done",
           "elapsed": (datetime.now() - t0).total_seconds()}


# ── Resumen ejecutivo para el reporte diario (alerts/watcher.py:_daily_reports) ──
SUMMARY_SYSTEM_PROMPT = """Eres un asistente que genera resúmenes ejecutivos breves para un reporte diario de monitoreo de medios (TV/radio) en español.

Reglas:
- Resume en 3-5 frases los temas/patrones más relevantes del día para esta búsqueda -- no listes cada coincidencia una por una, el reporte ya incluye la tabla completa.
- Agrupa coincidencias relacionadas (mismo tema, mismo canal, mismo horario) en vez de repetir.
- Si algo destaca (pico de menciones, canal dominante, tono evidente en el texto), señálalo.
- No inventes información que no esté en las coincidencias dadas.
- Responde en español, tono neutral y profesional."""

# ~1 token ≈ 4 caracteres en español -- deja margen dentro de LLM_CTX=4096
# para el system prompt + la respuesta generada (max_tokens), sin necesidad
# de contar tokens exactamente.
SUMMARY_CONTEXT_CHAR_BUDGET = 3000


def summarize_matches(search_name: str, matches: list[dict], max_tokens: int = 300) -> str:
    """Resumen ejecutivo de las coincidencias de UN día para una búsqueda
    (ver alerts/watcher.py _daily_reports). Llamada síncrona, NO streaming
    -- a diferencia de ask_stream (pensada para la UI de preguntas), aquí se
    llama una vez por búsqueda 'daily' activa, una vez al día."""
    if not matches:
        return ""
    lines, used, omitted = [], 0, 0
    for m in matches:
        ts  = str(m.get("timestamp", ""))[11:19]
        ch  = m.get("channel_name", "—")
        kw  = m.get("keyword", "")
        txt = (m.get("matched_text") or "")[:200]
        line = f"- [{ts}] {ch} · «{kw}»: {txt}"
        if used + len(line) > SUMMARY_CONTEXT_CHAR_BUDGET:
            omitted += 1
            continue
        lines.append(line)
        used += len(line)
    if omitted:
        lines.append(f"(+{omitted} coincidencias adicionales no incluidas en este resumen)")
    context = "\n".join(lines)
    user_prompt = (
        f"Búsqueda: {search_name}\nTotal de coincidencias hoy: {len(matches)}\n\n"
        f"COINCIDENCIAS:\n{context}\n\nRESUMEN EJECUTIVO:"
    )
    try:
        llm = get_llm()
        resp = llm.create_chat_completion(
            messages=[
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=0.2,
        )
        return resp["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"(resumen no disponible: {e})"
