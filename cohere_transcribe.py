#!/usr/bin/env python3
"""
cohere_transcribe.py — Transcripción OFFLINE con Cohere Transcribe (03-2026).

AISLADO del pipeline de producción (venv-cohere). Para el A/B contra Parakeet/Qwen.

VENTAJA sobre Parakeet: Cohere SÍ permite forzar el idioma (`language="es"`), así
que no debería tener code-switching aunque se troceé en ventanas cortas. Es un AED
(encoder-decoder generativo) de 2B params, Apache 2.0.

NOTA: como AED, "es ávido de transcribir incluso ruido/silencio" (model card) →
tiende a alucinar sobre no-voz; en producción querría VAD delante. Aquí lo medimos
tal cual sobre el clip.

Uso:
    ./venv-cohere/bin/python cohere_transcribe.py <audio> [--chunk 30] [--lang es] [--max-min N]
"""
import sys
import os
import time
import argparse
import subprocess
import re
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
MODEL_ID = "CohereLabs/cohere-transcribe-03-2026"
OUT_DIR = Path("output_cohere")

_EN_RE = re.compile(
    r'\b(the|of|and|with|that|this|has|been|was|were|are|is|for|from|to|in|on|'
    r'more|during|first|report|potential|actually|already|million|thousand|'
    r'converted|principal|causes|common|niche|diverse|region|which|there|their)\b',
    re.IGNORECASE)


def extract_audio(path: str) -> np.ndarray:
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-err_detect", "ignore_err",
           "-i", path, "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "pipe:1"]
    proc = subprocess.run(cmd, capture_output=True)
    if not proc.stdout:
        sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
        raise RuntimeError(f"ffmpeg no pudo extraer audio de {path}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def fmt_ts(sec: float) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio_file")
    ap.add_argument("--chunk", type=int, default=30, help="segundos por ventana (default 30)")
    ap.add_argument("--lang", default="es", help="idioma forzado (default es)")
    ap.add_argument("--max-min", type=float, default=0, help="limita a los primeros N min")
    args = ap.parse_args()

    src = args.audio_file
    if not os.path.exists(src):
        sys.exit(f"No existe: {src}")

    print(f"[1/4] Extrayendo audio de {src} …", flush=True)
    audio = extract_audio(src)
    if args.max_min > 0:
        audio = audio[:int(args.max_min * 60 * SAMPLE_RATE)]
    total_sec = len(audio) / SAMPLE_RATE
    print(f"      audio {total_sec:.1f}s ({fmt_ts(total_sec)})", flush=True)

    win = args.chunk * SAMPLE_RATE
    chunks = [audio[i:i + win] for i in range(0, len(audio), win)]
    if chunks and len(chunks[-1]) < SAMPLE_RATE:
        chunks.pop()
    print(f"[2/4] {len(chunks)} ventanas de {args.chunk}s  ·  idioma forzado='{args.lang}'", flush=True)

    print(f"[3/4] Cargando {MODEL_ID} …", flush=True)
    t_load = time.time()
    import warnings; warnings.filterwarnings("ignore")
    import torch
    from transformers import AutoProcessor, CohereAsrForConditionalGeneration
    # IMPORTANTE: procesador NATIVO (trust_remote_code=False). El código remoto del
    # repo produce las features transpuestas e incompatibles con el modelo nativo de
    # transformers 5.14 (da 'mat1 and mat2 shapes cannot be multiplied'). El nativo
    # entrega [B, time, mel] + decoder_input_ids con el idioma forzado.
    processor = AutoProcessor.from_pretrained(MODEL_ID, trust_remote_code=False)
    model = CohereAsrForConditionalGeneration.from_pretrained(
        MODEL_ID, trust_remote_code=True, device_map="auto")
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    gpu = torch.cuda.get_device_name(0) if dev == "cuda" else "CPU"
    print(f"      cargado en {time.time()-t_load:.1f}s · device={dev} ({gpu}) · dtype={model.dtype}", flush=True)

    print(f"[4/4] Transcribiendo (idioma={args.lang}) …\n", flush=True)
    OUT_DIR.mkdir(exist_ok=True)
    out_path = OUT_DIR / (Path(src).stem + ".cohere.txt")

    lines = []
    infer_sec = 0.0
    for i, ch in enumerate(chunks):
        inputs = processor(ch, sampling_rate=SAMPLE_RATE, return_tensors="pt", language=args.lang)
        inputs = inputs.to(model.device, dtype=model.dtype)
        t0 = time.time()
        out = model.generate(**inputs, max_new_tokens=256)
        infer_sec += time.time() - t0
        text = processor.batch_decode(out, skip_special_tokens=True)[0].strip()
        start = i * args.chunk
        if text:
            lines.append((start, start + args.chunk, text))
            print(f"[{fmt_ts(start)} → {fmt_ts(start+args.chunk)}] {text}", flush=True)

    full = " ".join(t for _, _, t in lines)
    nw = max(1, len(full.split()))
    en = len(_EN_RE.findall(full))
    rtfx = total_sec / infer_sec if infer_sec else 0

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"# Cohere Transcribe 03-2026 · chunk={args.chunk}s lang={args.lang} · {gpu}\n")
        f.write(f"# audio {total_sec:.0f}s · inferencia {infer_sec:.1f}s · RTFx {rtfx:.1f}x "
                f"· EN≈{en} ({100*en/nw:.1f}%)\n\n")
        for start, end, text in lines:
            f.write(f"[{fmt_ts(start)} → {fmt_ts(end)}] {text}\n")

    print(f"\n{'='*72}")
    print(f"RESUMEN Cohere Transcribe · chunk={args.chunk}s · lang={args.lang} · {gpu}")
    print(f"  audio             : {total_sec:.1f}s ({fmt_ts(total_sec)})")
    print(f"  inferencia        : {infer_sec:.1f}s  →  RTFx {rtfx:.1f}x tiempo real")
    print(f"  canales estimados : ~{int(rtfx)} simultáneos (mínimo: 8)")
    print(f"  code-switching    : ~{en} palabras EN de {nw} ({100*en/nw:.1f}%)")
    print(f"  salida            : {out_path}")
    print(f"{'='*72}")


if __name__ == "__main__":
    main()
