#!/usr/bin/env python3
"""
finalize_video.py — Convierte bloques de grabación ya CERRADOS (.ts o .mkv)
a MP4 limpio (`+faststart`, listo para servir sin procesar) — remux puro,
sin recodificar (mismo códec de origen, H264/.ts en canales CPU o
AV1/.mkv en canales GPU, audio ya en AAC).

video_recorder.py sigue grabando .ts (CPU/H264) o .mkv (GPU/AV1) como
formato EN VIVO -- ver el plan del 2026-08-10: MP4 fragmentado en vivo
rompe el Videowall (lee el segmento activo con `-sseof`), y MPEG-TS no
soporta AV1 de forma legible, por eso los canales GPU usan Matroska en
vez de .ts. Este script solo toca los bloques que YA terminaron de
grabarse, sin importar cuál de los dos formatos de origen tengan.

Criterio de "segmento activo" (nunca se toca): el .ts/.mkv con mtime más
reciente en cada carpeta de canal -- mismo criterio que ya usa
alerts/videowall.py:_latest_segment(), la única fuente de verdad que
existía para esto antes de este script.

Uso:
    python3 finalize_video.py                 # corre una pasada
    python3 finalize_video.py --dry-run        # solo muestra qué haría
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

VIDEO_DIR = Path(os.environ.get("TRANSCRIBER_VIDEO_DIR", "/home/transcriber/transcriber-linux/output_video"))
_SRC_EXTS = ("*.ts", "*.mkv")


def _latest_segment(folder: Path) -> Path | None:
    segments = [p for pat in _SRC_EXTS for p in folder.glob(pat)]
    if not segments:
        return None
    return max(segments, key=lambda p: p.stat().st_mtime)


def _finalize(ts_path: Path, dry_run: bool) -> bool:
    mp4_path = ts_path.with_suffix(".mp4")
    if dry_run:
        print(f"[dry-run] finalizaría {ts_path} -> {mp4_path.name}")
        return True
    tmp_path = mp4_path.with_suffix(".mp4.tmp")
    cmd = ["ffmpeg", "-y", "-i", str(ts_path),
           "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
           "-c", "copy", "-movflags", "+faststart",
           # -f mp4 explícito: el archivo temporal termina en .tmp, no en
           # .mp4 (rename atómico al final) -- sin esto ffmpeg no puede
           # adivinar el formato de salida por la extensión.
           "-f", "mp4", str(tmp_path), "-loglevel", "error"]
    try:
        r = subprocess.run(cmd, timeout=120)
    except subprocess.TimeoutExpired:
        print(f"  timeout finalizando {ts_path.name}", file=sys.stderr)
        tmp_path.unlink(missing_ok=True)
        return False
    if r.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size == 0:
        print(f"  falló remux de {ts_path.name}", file=sys.stderr)
        tmp_path.unlink(missing_ok=True)
        return False
    tmp_path.replace(mp4_path)  # rename atómico -- nunca se sirve un mp4 a medias
    ts_path.unlink()
    print(f"  {ts_path.name} -> {mp4_path.name}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not VIDEO_DIR.exists():
        print(f"No existe {VIDEO_DIR}, nada que hacer.")
        return

    total = 0
    for folder in sorted(VIDEO_DIR.iterdir()):
        if not folder.is_dir() or not folder.name.startswith("canal_"):
            continue
        active = _latest_segment(folder)
        pending = sorted(p for pat in _SRC_EXTS for p in folder.glob(pat))
        for src_path in pending:
            if src_path == active:
                continue  # el recorder todavía lo tiene abierto
            if _finalize(src_path, args.dry_run):
                total += 1

    print(f"{'Finalizaría' if args.dry_run else 'Finalizados'} {total} bloques.")


if __name__ == "__main__":
    main()
