#!/usr/bin/env python3
"""
parakeet_stream.py — Prototipo de VENTANA DESLIZANTE con solapamiento para
Parakeet-TDT-0.6B-v3, pensado para broadcast CONTINUO 24x7.

AISLADO del pipeline de producción (venv-parakeet). Simula streaming sobre un
archivo para validar la lógica antes de conectarla al sistema en vivo.

PROBLEMA que resuelve (medido 2026-07-17): Parakeet se cambia solo al inglés
cuando una ventana arranca "en frío", sin contexto español previo. En señal
continua no hay "fin de clip", así que cortar en ventanas contiguas hace que cada
una arranque en frío y derive en sus primeros segundos.

SOLUCIÓN: cada paso transcribe [CONTEXTO izquierdo ya emitido] + [HOP nuevo], con
atención completa, pero solo CONFIRMA los segmentos cuyo inicio cae en la región
nueva. El contexto precalienta el idioma; el solapamiento se deduplica por tiempo
de inicio (cada segmento se emite exactamente una vez).

    ventana del paso k:   [ ctx ......... | hop ......... ]
                          w_start        t_k          t_k+hop
    se CONFIRMAN solo los segmentos con inicio_absoluto >= t_k

Compromiso: latencia ≈ hop; costo de cómputo ≈ (ctx+hop)/hop (el contexto se
re-transcribe cada paso). Con hop=2min/ctx=2min → 2x cómputo, 2min latencia.

Uso:
    ./venv-parakeet/bin/python parakeet_stream.py <audio> [--hop-min 2] [--context-min 2] [--max-min N]
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
    ap.add_argument("--hop-min", type=float, default=2.0,
                    help="min de audio NUEVO por paso ≈ latencia (default 2)")
    ap.add_argument("--context-min", type=float, default=2.0,
                    help="min de contexto izquierdo re-transcrito para anclar idioma (default 2)")
    ap.add_argument("--max-min", type=float, default=0, help="limita a los primeros N min (0=todo)")
    args = ap.parse_args()

    src = args.audio_file
    if not os.path.exists(src):
        sys.exit(f"No existe: {src}")

    print(f"[1/3] Extrayendo audio de {src} …", flush=True)
    audio = extract_audio(src)
    if args.max_min > 0:
        audio = audio[:int(args.max_min * 60 * SAMPLE_RATE)]
    total_sec = len(audio) / SAMPLE_RATE

    hop = args.hop_min * 60.0
    ctx = args.context_min * 60.0
    n_hops = max(1, int(np.ceil(total_sec / hop)))
    overhead = (ctx + hop) / hop
    print(f"      audio {total_sec:.1f}s ({fmt_ts(total_sec)})", flush=True)
    print(f"[2/3] Deslizante: hop={args.hop_min}min  ctx={args.context_min}min  "
          f"→ {n_hops} pasos · latencia≈{args.hop_min}min · cómputo≈{overhead:.1f}x", flush=True)

    import warnings; warnings.filterwarnings("ignore")
    import torch
    import nemo.collections.asr as nemo_asr
    model = nemo_asr.models.ASRModel.from_pretrained(model_name=MODEL_ID)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model = model.to(dev)
    gpu = torch.cuda.get_device_name(0) if dev == "cuda" else "CPU"

    print(f"[3/3] Transcribiendo (streaming simulado) …\n", flush=True)
    OUT_DIR.mkdir(exist_ok=True)
    out_path = OUT_DIR / (Path(src).stem + ".stream.txt")

    committed = []      # (start_abs, end_abs, text) confirmados una sola vez
    infer_sec = 0.0
    for k in range(n_hops):
        t_k = k * hop                          # inicio de la región NUEVA (commit boundary)
        w_start = max(0.0, t_k - ctx)          # inicio de la ventana (con contexto)
        w_end = min(total_sec, t_k + hop)      # fin de la ventana
        a = audio[int(w_start * SAMPLE_RATE):int(w_end * SAMPLE_RATE)]
        if len(a) < SAMPLE_RATE:
            continue

        t0 = time.time()
        hyp = model.transcribe([a], batch_size=1, verbose=False, timestamps=True)[0]
        infer_sec += time.time() - t0

        segs = getattr(hyp, "timestamp", None)
        segs = segs.get("segment") if isinstance(segs, dict) else None
        kept = 0
        if segs:
            for s in segs:
                start_abs = w_start + s["start"]
                end_abs = w_start + s["end"]
                txt = (s.get("segment") or "").strip()
                # CONFIRMAR solo si el segmento INICIA en la región nueva [t_k, t_k+hop).
                # (k==0 no tiene región previa: se confirma todo.)
                if txt and (k == 0 or start_abs >= t_k - 0.01):
                    committed.append((start_abs, end_abs, txt))
                    kept += 1
        print(f"  paso {k+1}/{n_hops}  ventana [{fmt_ts(w_start)}–{fmt_ts(w_end)}]  "
              f"confirma {kept} seg desde {fmt_ts(t_k)}", flush=True)

    committed.sort(key=lambda x: x[0])
    full = " ".join(t for _, _, t in committed)
    nw = max(1, len(full.split()))
    en = len(_EN_RE.findall(full))
    rtfx = total_sec / infer_sec if infer_sec else 0
    eff_rtfx = rtfx  # ya incluye el re-cómputo del contexto (RTFx efectivo real)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"# Parakeet stream · hop={args.hop_min}min ctx={args.context_min}min · {gpu}\n")
        f.write(f"# audio {total_sec:.0f}s · inferencia {infer_sec:.1f}s · RTFx {rtfx:.1f}x "
                f"· cómputo {overhead:.1f}x · EN≈{en} ({100*en/nw:.1f}%)\n\n")
        for start, end, text in committed:
            f.write(f"[{fmt_ts(start)} → {fmt_ts(end)}] {text}\n")

    print(f"\n{'='*74}")
    print(f"RESUMEN VENTANA DESLIZANTE · hop={args.hop_min}min ctx={args.context_min}min · {gpu}")
    print(f"  audio             : {total_sec:.1f}s ({fmt_ts(total_sec)})  ·  {len(committed)} segmentos")
    print(f"  latencia          : ~{args.hop_min:.0f} min (= hop)")
    print(f"  RTFx efectivo     : {eff_rtfx:.1f}x  (incluye re-cómputo del contexto, {overhead:.1f}x)")
    print(f"  canales estimados : ~{int(eff_rtfx)} simultáneos (mínimo: 8)")
    print(f"  code-switching    : ~{en} palabras EN de {nw} ({100*en/nw:.1f}%)")
    print(f"  salida            : {out_path}")
    print(f"{'='*74}")


if __name__ == "__main__":
    main()
