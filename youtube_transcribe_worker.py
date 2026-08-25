#!/usr/bin/env python3
"""
youtube_transcribe_worker.py — Subproceso que transcribe un WAV con el motor
CTC-ES por CPU. Corre bajo venv-parakeet (el único venv con NeMo/torch/
onnxruntime instalados) aunque lo invoque alerts/youtube.py desde el venv
del watcher (que no los tiene). device="cpu" fuerza CPUExecutionProvider
(ver transcriber_ctc_es.py _load_model), así que aunque venv-parakeet tenga
CUDA en el PATH, esto nunca toca la GPU -- no compite con Parakeet/Cohere.

Uso: venv-parakeet/bin/python3 youtube_transcribe_worker.py <wav_path>
Salida: JSON en stdout, [[offset_seg, texto], ...]
"""
import sys
import json
import logging
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

SAMPLE_RATE = 16000
CHUNK_SEC = 30

logging.basicConfig(level=logging.WARNING)  # silencio en stdout -- solo el JSON final
logger = logging.getLogger("youtube_transcribe_worker")


def main():
    wav_path = Path(sys.argv[1])
    import transcriber_ctc_es as ctc

    preprocessor, session, vocab, blank_id = ctc._load_model(logger, device="cpu")

    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getframerate() == SAMPLE_RATE, f"sample rate inesperado: {wf.getframerate()}"
        raw = wf.readframes(wf.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    window = CHUNK_SEC * SAMPLE_RATE
    segments = []
    for start in range(0, len(audio), window):
        chunk = audio[start:start + window]
        if len(chunk) < SAMPLE_RATE:
            continue
        text = ctc.transcribe_chunk(preprocessor, session, vocab, blank_id, chunk)
        if text:
            segments.append([start / SAMPLE_RATE, text])

    print(json.dumps(segments))


if __name__ == "__main__":
    main()
