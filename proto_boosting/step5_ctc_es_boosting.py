"""
Prototipo de refuerzo de vocabulario para CTC-ES (TV), usando el
tokenizador aproximado (ver approx_tokenizer.py) ya que el original no
sobrevivió a la exportación a ONNX. Reproduce el pipeline de
transcriber_ctc_es.py tal cual (mismo preprocesador NeMo + grafo ONNX +
decodificador greedy) para obtener logprobs reales, y aplica el
word-spotter de NeMo (context_biasing) sobre esos logprobs para intentar
corregir un error real documentado ("Sheinbaum" mal transcrito de al menos
7 formas distintas en canales de noticias).
"""
import sys
import time

import numpy as np
import onnxruntime as ort
import torch
import yaml

sys.path.insert(0, "proto_boosting")
from approx_tokenizer import load_vocab, tokenize_phrase

from nemo.collections.asr.parts.context_biasing.context_graph_ctc import ContextGraphCTC
from nemo.collections.asr.parts.context_biasing.ctc_based_word_spotter import run_word_spotter
from nemo.collections.asr.parts.context_biasing.context_biasing_utils import merge_alignment_with_ws_hyps

MODEL_DIR = "models/parakeet-ctc-es"
WAV = "proto_boosting/test_tv_sheimbaum.wav"
SAMPLE_RATE = 16000


class _MockTokenizer:
    """Solo implementa lo que el word-spotter/merge realmente usan:
    ids_to_tokens(). No hace falta encode() real -- eso ya lo cubre
    approx_tokenizer.tokenize_phrase() por separado."""
    def __init__(self, vocab):
        self.vocab = vocab

    def ids_to_tokens(self, ids):
        return [self.vocab[i] for i in ids]


class _MockASRModel:
    def __init__(self, vocab):
        self.tokenizer = _MockTokenizer(vocab)


def load_ctc_es():
    with open(f"{MODEL_DIR}/model_config.yaml") as f:
        cfg = yaml.safe_load(f)
    vocab = cfg["decoder"]["vocabulary"]
    blank_id = len(vocab)

    from nemo.collections.asr.modules import AudioToMelSpectrogramPreprocessor
    pp_cfg = dict(cfg["preprocessor"])
    pp_cfg.pop("_target_", None)
    preprocessor = AudioToMelSpectrogramPreprocessor(**pp_cfg)

    so = ort.SessionOptions()
    session = ort.InferenceSession(f"{MODEL_DIR}/model_graph_fixed.onnx", sess_options=so,
                                    providers=["CPUExecutionProvider"])
    return preprocessor, session, vocab, blank_id


def get_logprobs(preprocessor, session, audio: np.ndarray) -> np.ndarray:
    audio_t = torch.tensor(audio, dtype=torch.float32).unsqueeze(0)
    length_t = torch.tensor([audio_t.shape[1]], dtype=torch.int64)
    feats, feat_len = preprocessor(input_signal=audio_t, length=length_t)
    feats_np = feats.numpy()
    feat_len_np = feat_len.numpy()
    out = session.run(None, {session.get_inputs()[0].name: feats_np,
                              session.get_inputs()[1].name: feat_len_np})
    return out[0][0]  # [time, vocab+blank]


def greedy_decode(logprobs: np.ndarray, vocab, blank_id: int) -> str:
    ids = np.argmax(logprobs, axis=-1)
    tokens = []
    prev = None
    for i in ids:
        if i != blank_id and i != prev:
            tokens.append(vocab[i])
        prev = i
    return "".join(tokens).replace("▁", " ").strip()


print("Cargando CTC-ES (ONNX, CPU -- igual que producción)...")
preprocessor, session, vocab, blank_id = load_ctc_es()
print(f"vocabulario={len(vocab)} blank_id={blank_id}")

import soundfile as sf
audio, sr = sf.read(WAV)
assert sr == SAMPLE_RATE
audio = audio.astype(np.float32)

t0 = time.time()
logprobs = get_logprobs(preprocessor, session, audio)
print(f"logprobs calculados en {time.time()-t0:.1f}s, forma={logprobs.shape}")

baseline_text = greedy_decode(logprobs, vocab, blank_id)
print(f"\n=== BASELINE (greedy, igual que producción) ===\n{baseline_text}")

print("\n=== Construyendo grafo de refuerzo ('Sheinbaum') con tokenizador aproximado ===")
mock_model = _MockASRModel(vocab)
graph = ContextGraphCTC(blank_id=blank_id)
tokenizations = tokenize_phrase("Sheinbaum", vocab)
print(f"tokenizaciones candidatas: {tokenizations}")
graph.add_to_graph([("Sheinbaum", tokenizations)])

t0 = time.time()
ws_results = run_word_spotter(logprobs, graph, mock_model, blank_idx=blank_id)
print(f"word-spotter corrió en {time.time()-t0:.1f}s -- {len(ws_results)} hipótesis encontradas")
for h in ws_results:
    print(" ", h)

if not ws_results:
    print("\n(0 hipótesis -- nada que fusionar; el word-spotter no encontró la frase en este audio)")
else:
    print("\n=== Fusionando con la transcripción base ===")
    ids_per_frame = np.argmax(logprobs, axis=-1)
    boosted_text, _ = merge_alignment_with_ws_hyps(
        ids_per_frame, mock_model, ws_results, decoder_type="ctc", blank_idx=blank_id, print_stats=True,
    )
    print(f"\n=== RESULTADO CON REFUERZO ===\n{boosted_text}")
