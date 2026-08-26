"""
music_classifier.py — Detección de música por audio (no por texto), para
marcar transcripciones que sin lugar a dudas tienen música de fondo, sin
descartarlas -- un comercial o spot con música de fondo puede decir algo
relevante y debe seguir siendo buscable, solo marcado.

Usa YAMNet (Google, MobileNet -- liviano a propósito) exportado a ONNX, NO
un transformer tipo AST/PANNs: en la prueba real, AST tardaba 2.7-4.9s por
fragmento de 30s en CPU (con 64 canales, ~8-9 núcleos dedicados solo para
esto, inviable). YAMNet ONNX tarda ~100ms por fragmento -- con 64 canales a
un fragmento cada 30s, eso es ~0.2 núcleos en promedio.

Corre por CPU siempre (CPUExecutionProvider fijo) -- nunca debe competir
por la GPU con Parakeet/Cohere, que ya la tienen muy ajustada (ver
video_recorder.py y el incidente de OOM documentado ahí).
"""
import csv
import os
from pathlib import Path

import numpy as np

BASE_DIR   = Path(__file__).parent
MODEL_DIR  = Path(os.environ.get("MUSIC_CLASSIFIER_MODEL_DIR", str(BASE_DIR / "models" / "yamnet")))
ONNX_FILE  = MODEL_DIR / "yamnet.onnx"
CLASS_FILE = MODEL_DIR / "yamnet_class_map.csv"
SAMPLE_RATE = 16000

# Umbral conservador a propósito -- "sin lugar a dudas es música", no
# "podría tener algo de música". Un comercial con música de fondo pero voz
# clara (el caso que NO se quiere marcar) midió Music=0.22/Speech=0.66 en
# pruebas reales; música pura midió Music=0.73/Speech=0.09. 0.5 separa bien
# ambos casos con margen.
MUSIC_THRESHOLD = float(os.environ.get("MUSIC_CLASSIFIER_THRESHOLD", "0.5"))

_session = None
_class_names = None
_music_idx = None
_speech_idx = None


def _load():
    global _session, _class_names, _music_idx, _speech_idx
    if _session is not None:
        return
    import onnxruntime as ort

    with open(CLASS_FILE) as f:
        _class_names = [row[2] for row in csv.reader(f)][1:]  # salta encabezado
    _music_idx  = _class_names.index("Music")
    _speech_idx = _class_names.index("Speech")

    so = ort.SessionOptions()
    so.intra_op_num_threads = int(os.environ.get("MUSIC_CLASSIFIER_THREADS", "2"))
    _session = ort.InferenceSession(str(ONNX_FILE), sess_options=so, providers=["CPUExecutionProvider"])


def music_score(audio: np.ndarray) -> float:
    """audio: float32 mono a 16kHz (mismo formato que ya usan los motores
    ASR). Devuelve el score promedio de la clase "Music" de YAMNet (0-1)."""
    _load()
    out = _session.run(None, {_session.get_inputs()[0].name: audio.astype(np.float32)})
    scores = out[0]  # (ventanas de ~0.96s, 521 clases)
    if scores.shape[0] == 0:
        return 0.0
    return float(scores.mean(axis=0)[_music_idx])


def is_music(audio: np.ndarray, threshold: float = MUSIC_THRESHOLD) -> bool:
    try:
        return music_score(audio) >= threshold
    except Exception:
        # Nunca debe tronar el pipeline de transcripción en vivo por esto --
        # sin marca de música es el fallback seguro (no se pierde el texto).
        return False
