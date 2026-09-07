import copy
import time

import torch
from omegaconf import OmegaConf

import nemo.collections.asr as nemo_asr

WAV = "proto_boosting/test_radio_iteso.wav"

print("Cargando Parakeet-TDT en CPU (sin tocar la GPU)...")
model = nemo_asr.models.ASRModel.from_pretrained(
    model_name="nvidia/parakeet-tdt-0.6b-v3",
    map_location=torch.device("cpu"),
)
model.eval()

print("\n=== Transcripción SIN refuerzo (baseline) ===")
t0 = time.time()
baseline = model.transcribe([WAV])
print(f"({time.time()-t0:.1f}s) ->", baseline[0].text if hasattr(baseline[0], "text") else baseline[0])

print("\n=== Configurando boosting_tree ===")
decoding_cfg = copy.deepcopy(model.cfg.decoding)
OmegaConf.set_struct(decoding_cfg, False)
OmegaConf.set_struct(decoding_cfg.greedy, False)
decoding_cfg.strategy = "greedy_batch"
decoding_cfg.greedy.boosting_tree_alpha = 5.0
decoding_cfg.greedy.boosting_tree = OmegaConf.create({
    "key_phrases_list": ["radio ITESO"],
    "context_score": 3.0,
    "depth_scaling": 2.0,
    "use_triton": False,   # CPU -- clave para el prototipo, sin depender de kernels GPU
    "source_lang": "es",
})

try:
    model.change_decoding_strategy(decoding_cfg)
    print("change_decoding_strategy OK")
except Exception as e:
    print("ERROR en change_decoding_strategy:", repr(e))
    raise

print("\n=== Transcripción CON refuerzo ('radio ITESO') ===")
t0 = time.time()
boosted = model.transcribe([WAV])
print(f"({time.time()-t0:.1f}s) ->", boosted[0].text if hasattr(boosted[0], "text") else boosted[0])
