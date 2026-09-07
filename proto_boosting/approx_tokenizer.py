"""
approx_tokenizer.py — Tokenizador BPE aproximado para CTC-ES, reconstruido a
partir de SOLO la lista de vocabulario (el archivo original del tokenizador
SentencePiece no sobrevivió a la exportación a ONNX -- ver la nota en
transcriber_ctc_es.py). Usa coincidencia-más-larga (greedy longest-match)
de izquierda a derecha contra el vocabulario, convención SentencePiece
("▁" marca inicio de palabra) -- una aproximación razonable cuando no se
tiene el modelo BPE entrenado original, suficiente para construir el grafo
de refuerzo de NeMo (ContextGraphCTC), que de por sí acepta MÚLTIPLES
tokenizaciones candidatas por palabra para compensar imprecisión.
"""
import yaml


def load_vocab(config_path: str) -> list[str]:
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    return cfg["decoder"]["vocabulary"]


def _greedy_tokenize(text: str, vocab_set: set, max_len: int) -> list[str] | None:
    """None si algún punto no se pudo cubrir con ningún token del vocabulario."""
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


def tokenize_phrase(phrase: str, vocab: list[str]) -> list[list[int]]:
    """Devuelve una lista de tokenizaciones candidatas (cada una, una lista
    de IDs de token) para `phrase` -- prueba variantes de mayúsculas/
    minúsculas porque el vocabulario es bilingüe es/en con casing mixto."""
    vocab_set = set(vocab)
    id_of = {tok: i for i, tok in enumerate(vocab)}
    max_len = max(len(t) for t in vocab)

    words = phrase.split()
    candidates_text = []
    # Variante 1: todo en minúsculas (dominante en el vocab para español)
    candidates_text.append("".join("▁" + w.lower() for w in words))
    # Variante 2: capitalización tal cual vino (por si el token existe así, ej. nombres propios)
    candidates_text.append("".join("▁" + w for w in words))

    results = []
    seen = set()
    for text in candidates_text:
        toks = _greedy_tokenize(text, vocab_set, max_len)
        if toks is None:
            continue
        ids = tuple(id_of[t] for t in toks)
        if ids not in seen:
            seen.add(ids)
            results.append(list(ids))
    return results


if __name__ == "__main__":
    vocab = load_vocab("models/parakeet-ctc-es/model_config.yaml")
    print(f"vocabulario: {len(vocab)} tokens")
    for phrase in ["radio ITESO", "Excélsior", "Sheinbaum", "Rochamoya"]:
        cands = tokenize_phrase(phrase, vocab)
        print(f"\n'{phrase}':")
        if not cands:
            print("  SIN cobertura -- no se pudo tokenizar con el vocabulario disponible")
        for ids in cands:
            pieces = [vocab[i] for i in ids]
            print(f"  {pieces}  (ids={ids})")
