#!/usr/bin/env python3
"""
parakeet_transcribe.py — Transcripción OFFLINE con NVIDIA Parakeet-TDT-0.6B-v3.
MODO VENTANA LARGA (anti code-switching).

AISLADO del pipeline de producción: corre en venv-parakeet, no toca el sistema
Qwen en vivo. Sirve para el A/B de calidad/velocidad sobre audio real.

CLAVE (medido 2026-07-17): Parakeet se cambia solo al inglés cuando se le dan
ventanas cortas y aisladas de 30s. Con ATENCIÓN COMPLETA y ventanas de varios
minutos, el desvío de idioma casi desaparece (de ~38 a ~5 palabras EN en el clip
de prueba). No existe flag oficial para forzar español; la palanca real es el
CONTEXTO. Por eso este script NO trocea en 30s: procesa ventanas largas y usa los
timestamps de segmento del modelo para reconstruir líneas con tiempo real.

Uso:
    ./venv-parakeet/bin/python parakeet_transcribe.py <archivo> [--window-min 4] [--max-min N] [--attn full|local]

--window-min : minutos de audio por ventana enviada al modelo (default 4).
               Más grande = más contexto (menos code-switching) pero más VRAM.
--attn       : 'full' (default, mejor calidad) o 'local' (menos VRAM para audio muy largo).
--max-min    : limita a los primeros N minutos (0 = todo).
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
MODEL_ID = "nvidia/parakeet-tdt-0.6b-v3"
OUT_DIR = Path("output_parakeet")

# Palabras claramente inglesas — solo para el diagnóstico de code-switching del A/B.
_EN_RE = re.compile(
    r'\b(the|of|and|with|that|this|has|been|was|were|are|is|for|from|to|in|on|'
    r'more|during|first|report|potential|actually|already|million|thousand|'
    r'converted|principal|causes|common|niche|diverse|region|which|there|their)\b',
    re.IGNORECASE)


def extract_audio(path: str) -> np.ndarray:
    """Extrae audio a 16 kHz mono float32 con ffmpeg (tolerante a headers rotos)."""
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-err_detect", "ignore_err",
        "-i", path, "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-f", "f32le", "pipe:1",
    ]
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
    ap.add_argument("--window-min", type=float, default=4.0,
                    help="minutos por ventana enviada al modelo (default 4)")
    ap.add_argument("--max-min", type=float, default=0,
                    help="limita a los primeros N minutos (0 = todo)")
    ap.add_argument("--attn", choices=["full", "local"], default="full",
                    help="'full' (mejor calidad) o 'local' (menos VRAM)")
    args = ap.parse_args()

    src = args.audio_file
    if not os.path.exists(src):
        sys.exit(f"No existe: {src}")

    print(f"[1/4] Extrayendo audio de {src} …", flush=True)
    audio = extract_audio(src)
    if args.max_min > 0:
        audio = audio[:int(args.max_min * 60 * SAMPLE_RATE)]
    total_sec = len(audio) / SAMPLE_RATE
    print(f"      audio: {total_sec:.1f}s ({fmt_ts(total_sec)})  {len(audio):,} muestras", flush=True)

    win = int(args.window_min * 60 * SAMPLE_RATE)
    n_win = max(1, (len(audio) + win - 1) // win)
    print(f"[2/4] {n_win} ventana(s) de {args.window_min:.0f} min  ·  atención={args.attn}", flush=True)

    print(f"[3/4] Cargando {MODEL_ID} …", flush=True)
    t_load = time.time()
    import warnings; warnings.filterwarnings("ignore")
    import torch
    import nemo.collections.asr as nemo_asr
    model = nemo_asr.models.ASRModel.from_pretrained(model_name=MODEL_ID)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model = model.to(dev)
    if args.attn == "local":
        # Atención local: para audio muy largo que no cabe con atención completa.
        model.change_attention_model(self_attention_model="rel_pos_local_attn",
                                     att_context_size=[256, 256])
    gpu = torch.cuda.get_device_name(0) if dev == "cuda" else "CPU"
    print(f"      cargado en {time.time()-t_load:.1f}s  ·  device={dev} ({gpu})", flush=True)

    print(f"[4/4] Transcribiendo …\n", flush=True)
    OUT_DIR.mkdir(exist_ok=True)
    out_path = OUT_DIR / (Path(src).stem + ".parakeet.txt")

    lines = []          # (start_sec, end_sec, text)
    infer_sec = 0.0
    for w in range(n_win):
        seg_audio = audio[w * win:(w + 1) * win]
        if len(seg_audio) < SAMPLE_RATE:      # <1s, ignora cola
            continue
        offset = w * win / SAMPLE_RATE
        t0 = time.time()
        hyp = model.transcribe([seg_audio], batch_size=1, verbose=False,
                               timestamps=True)[0]
        infer_sec += time.time() - t0

        segs = getattr(hyp, "timestamp", None)
        segs = segs.get("segment") if isinstance(segs, dict) else None
        if segs:
            for s in segs:
                txt = (s.get("segment") or "").strip()
                if txt:
                    lines.append((offset + s["start"], offset + s["end"], txt))
        else:
            # Sin timestamps: una sola línea por ventana
            txt = (hyp.text if hasattr(hyp, "text") else str(hyp)).strip()
            if txt:
                lines.append((offset, offset + len(seg_audio) / SAMPLE_RATE, txt))
        print(f"  ventana {w+1}/{n_win} lista ({fmt_ts(offset)})", flush=True)

    # Diagnóstico de code-switching (solo informativo para el A/B)
    full_text = " ".join(t for _, _, t in lines)
    n_words = max(1, len(full_text.split()))
    en_hits = len(_EN_RE.findall(full_text))
    en_pct = 100.0 * en_hits / n_words

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"# Parakeet-TDT-0.6B-v3  ·  ventana={args.window_min}min attn={args.attn}\n")
        f.write(f"# fuente: {src}\n")
        f.write(f"# audio {total_sec:.0f}s · inferencia {infer_sec:.1f}s · "
                f"RTFx {total_sec/infer_sec:.1f}x · palabras-EN≈{en_hits} ({en_pct:.1f}%)\n\n")
        for start, end, text in lines:
            line = f"[{fmt_ts(start)} → {fmt_ts(end)}] {text}"
            print(line, flush=True)
            f.write(line + "\n")

    rtfx = total_sec / infer_sec if infer_sec > 0 else 0
    print(f"\n{'='*72}")
    print(f"RESUMEN Parakeet-TDT-0.6B-v3 · ventana={args.window_min}min · attn={args.attn} · {gpu}")
    print(f"  audio            : {total_sec:.1f}s ({fmt_ts(total_sec)})")
    print(f"  inferencia       : {infer_sec:.1f}s   →  RTFx {rtfx:.1f}x tiempo real")
    print(f"  canales estimados: ~{int(rtfx)} simultáneos (mínimo requerido: 8)")
    print(f"  code-switching   : ~{en_hits} palabras EN de {n_words} ({en_pct:.1f}%)  [menos es mejor]")
    print(f"  salida           : {out_path}")
    print(f"{'='*72}")


if __name__ == "__main__":
    main()
