import sys
sys.path.insert(0, "proto_boosting")
import numpy as np
import soundfile as sf

from approx_tokenizer import tokenize_phrase
from step5_ctc_es_boosting import load_ctc_es, get_logprobs, greedy_decode, _MockASRModel
from nemo.collections.asr.parts.context_biasing.context_graph_ctc import ContextGraphCTC
from nemo.collections.asr.parts.context_biasing.ctc_based_word_spotter import run_word_spotter
from nemo.collections.asr.parts.context_biasing.context_biasing_utils import merge_alignment_with_ws_hyps

WAV = "proto_boosting/test_tv_control.wav"

preprocessor, session, vocab, blank_id = load_ctc_es()
audio, sr = sf.read(WAV)
audio = audio.astype(np.float32)
logprobs = get_logprobs(preprocessor, session, audio)
baseline = greedy_decode(logprobs, vocab, blank_id)
print(f"=== BASELINE (control, sin 'Sheinbaum') ===\n{baseline}\n")

mock_model = _MockASRModel(vocab)
graph = ContextGraphCTC(blank_id=blank_id)
graph.add_to_graph([("Sheinbaum", tokenize_phrase("Sheinbaum", vocab))])
ws_results = run_word_spotter(logprobs, graph, mock_model, blank_idx=blank_id)
print(f"hipótesis encontradas en el control: {len(ws_results)}")
for h in ws_results:
    print(" ", h)

if ws_results:
    ids_per_frame = np.argmax(logprobs, axis=-1)
    boosted, _ = merge_alignment_with_ws_hyps(ids_per_frame, mock_model, ws_results, decoder_type="ctc", blank_idx=blank_id)
    print(f"\n=== RESULTADO CON REFUERZO (control) ===\n{boosted}")
else:
    print("(sin falsos positivos -- el refuerzo no insertó 'Sheinbaum' donde no corresponde)")
