import torch
import nemo.collections.asr as nemo_asr

model = nemo_asr.models.ASRModel.from_pretrained(
    model_name="nvidia/parakeet-tdt-0.6b-v3",
    map_location=torch.device("cpu"),
)
model.eval()
print("=== decoding cfg actual ===")
print(model.cfg.decoding)
print()
print("=== strategy ===")
print(model.cfg.decoding.get("strategy"))
