import copy
import time

import torch
from omegaconf import OmegaConf

import nemo.collections.asr as nemo_asr

CLIPS = {
    "positivo1": "proto_boosting/test_radio_iteso.wav",
    "positivo2": "proto_boosting/test_positivo2.wav",
    "control":   "proto_boosting/test_control.wav",
}

# Bisección entre 2.0 (sin efecto, confirmado) y 5.0 (alucina, confirmado)
CONFIGS = [
    {"alpha": 2.5, "context_score": 1.0},
    {"alpha": 3.0, "context_score": 1.0},
    {"alpha": 3.5, "context_score": 1.0},
    {"alpha": 4.0, "context_score": 1.0},
    {"alpha": 3.0, "context_score": 2.0},
]

print("Cargando Parakeet-TDT en CPU...")
model = nemo_asr.models.ASRModel.from_pretrained(
    model_name="nvidia/parakeet-tdt-0.6b-v3",
    map_location=torch.device("cpu"),
)
model.eval()
base_decoding_cfg = copy.deepcopy(model.cfg.decoding)
OmegaConf.set_struct(base_decoding_cfg, False)
OmegaConf.set_struct(base_decoding_cfg.greedy, False)

for cfg in CONFIGS:
    decoding_cfg = copy.deepcopy(base_decoding_cfg)
    decoding_cfg.strategy = "greedy_batch"
    decoding_cfg.greedy.boosting_tree_alpha = cfg["alpha"]
    decoding_cfg.greedy.boosting_tree = OmegaConf.create({
        "key_phrases_list": ["radio ITESO"],
        "context_score": cfg["context_score"],
        "depth_scaling": 2.0,
        "use_triton": False,
        "source_lang": "es",
    })
    model.change_decoding_strategy(decoding_cfg)

    print(f"\n########## alpha={cfg['alpha']} context_score={cfg['context_score']} ##########")
    for label, wav in CLIPS.items():
        t0 = time.time()
        out = model.transcribe([wav])
        text = out[0].text if hasattr(out[0], "text") else out[0]
        dt = time.time() - t0
        print(f"  [{label}] ({dt:.1f}s) {text}")

print("\nlisto")
