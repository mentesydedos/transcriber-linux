"""
ctc_es_boosting.py — Refuerzo de vocabulario para transcriber_ctc_es.py
(TV), usando el "word spotter" de contexto de NeMo (context_biasing) sobre
los logprobs que YA calcula el grafo ONNX en producción -- no cambia el
modelo ni la decodificación en vivo, es un paso posterior que busca
evidencia acústica fuerte de una palabra reforzada (nombre propio, sigla)
y, si la encuentra, reemplaza esa palabra en el texto ya decodificado.

Por qué esto y no lo mismo que en radio (ver text_corrections.py): aquí SÍ
hay una forma de reforzar el reconocimiento sin solo corregir texto a
ciegas -- el word-spotter exige evidencia acústica real en el audio (ver
proto_boosting/step6_ctc_es_control.py: 0 falsos positivos en una prueba de
control real), así que corrige casos que el diccionario simple no cubriría
bien (variantes de escritura que no se puede enumerar todas a mano). Se
probó lo mismo para radio (Parakeet-TDT) pero ahí el mecanismo equivalente
resultó inestable (ver proto_boosting/ y la nota en transcriber_parakeet.py)
-- por eso radio usa el diccionario simple y TV usa esto.

El tokenizador BPE original de este modelo no sobrevivió a la exportación
a ONNX (ver la nota en transcriber_ctc_es.py) -- se reconstruye una
aproximación por coincidencia-más-larga contra el vocabulario disponible
(ver approx_tokenizer, más abajo). El grafo de refuerzo (ContextGraphCTC)
solo necesita "id de token -> texto del token" para operar, no el
tokenizador BPE completo, así que un objeto simulado con esa única función
es suficiente -- ver _MockASRModel.

BOOSTED_WORDS_FILE: una palabra/frase por línea, líneas que empiezan con #
se ignoran. Se carga UNA vez al iniciar el proceso (construir el grafo es
barato pero no gratis, y la lista no cambia en caliente -- agregar
palabras requiere reiniciar el servicio, igual que cualquier otro cambio
de configuración de este motor)."""
import logging
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).parent
BOOSTED_WORDS_FILE = BASE_DIR / "models" / "parakeet-ctc-es" / "boosted_words.txt"

logger = logging.getLogger("ctc_es_boosting")


def _greedy_tokenize(text: str, vocab_set: set, max_len: int) -> list[str] | None:
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        matched = None
        for length in range(min(max_len, n - i), 0, -1):
            piece = text[i:i + length]
            if piece in vocab_set:
                matched = piece
                break
        if matched is None:
            return None
        tokens.append(matched)
        i += len(matched)
    return tokens


def _tokenize_phrase(phrase: str, vocab: list[str], id_of: dict) -> list[list[int]]:
    vocab_set = set(vocab)
    max_len = max(len(t) for t in vocab)
    words = phrase.split()
    candidates_text = [
        "".join("▁" + w.lower() for w in words),
        "".join("▁" + w for w in words),
    ]
    results, seen = [], set()
    for text in candidates_text:
        toks = _greedy_tokenize(text, vocab_set, max_len)
        if toks is None:
            continue
        ids = tuple(id_of[t] for t in toks)
        if ids not in seen:
            seen.add(ids)
            results.append(list(ids))
    return results


def _load_boosted_words() -> list[str]:
    if not BOOSTED_WORDS_FILE.exists():
        return []
    words = []
    for line in BOOSTED_WORDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.append(line)
    return words


class _MockTokenizer:
    """El word-spotter de NeMo solo llama ids_to_tokens() -- no hace falta
    un tokenizador BPE real para eso, solo la lista de vocabulario ya
    cargada por transcriber_ctc_es.py."""
    def __init__(self, vocab):
        self.vocab = vocab

    def ids_to_tokens(self, ids):
        return [self.vocab[i] for i in ids]


class _MockASRModel:
    def __init__(self, vocab):
        self.tokenizer = _MockTokenizer(vocab)


_state = None  # (graph, mock_model) o None si no hay palabras que reforzar / falló la carga


def init_boosting(vocab: list[str], blank_id: int):
    """Construye el grafo de refuerzo UNA vez, al cargar el modelo. Si algo
    falla (lista vacía, error de import de NeMo, etc.), deja el refuerzo
    desactivado -- transcriber_ctc_es.py sigue funcionando con greedy decode
    normal, igual que si esto no existiera."""
    global _state
    words = _load_boosted_words()
    if not words:
        logger.info("Sin palabras de refuerzo configuradas (%s vacío o inexistente) -- refuerzo desactivado.",
                     BOOSTED_WORDS_FILE)
        return
    try:
        from nemo.collections.asr.parts.context_biasing.context_graph_ctc import ContextGraphCTC
        id_of = {tok: i for i, tok in enumerate(vocab)}
        graph = ContextGraphCTC(blank_id=blank_id)
        added = 0
        for word in words:
            tokenizations = _tokenize_phrase(word, vocab, id_of)
            if tokenizations:
                graph.add_to_graph([(word, tokenizations)])
                added += 1
            else:
                logger.warning("No se pudo tokenizar la palabra de refuerzo '%s' -- se omite.", word)
        mock_model = _MockASRModel(vocab)
        _state = (graph, mock_model)
        logger.info("Refuerzo de vocabulario activo: %d/%d palabras cargadas.", added, len(words))
    except Exception as e:
        logger.error("No se pudo inicializar el refuerzo de vocabulario (%s) -- desactivado.", e)
        _state = None


def boost_text(logprobs: np.ndarray, baseline_text: str, blank_id: int) -> str:
    """Intenta mejorar baseline_text usando evidencia acústica en logprobs.
    Cualquier fallo devuelve baseline_text tal cual -- nunca debe frenar ni
    romper la transcripción en vivo por esto."""
    if _state is None:
        return baseline_text
    graph, mock_model = _state
    try:
        from nemo.collections.asr.parts.context_biasing.ctc_based_word_spotter import run_word_spotter
        from nemo.collections.asr.parts.context_biasing.context_biasing_utils import merge_alignment_with_ws_hyps

        ws_results = run_word_spotter(logprobs, graph, mock_model, blank_idx=blank_id)
        if not ws_results:
            return baseline_text
        ids_per_frame = np.argmax(logprobs, axis=-1)
        boosted_text, _ = merge_alignment_with_ws_hyps(
            ids_per_frame, mock_model, ws_results, decoder_type="ctc", blank_idx=blank_id,
        )
        return boosted_text.replace("▁", " ").strip() if boosted_text else baseline_text
    except Exception as e:
        logger.warning("Refuerzo de vocabulario falló en este fragmento (%s) -- se usa la transcripción normal.", e)
        return baseline_text
