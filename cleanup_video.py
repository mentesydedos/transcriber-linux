#!/usr/bin/env python3
"""
cleanup_video.py — Retención automática de output_video/: borra los segmentos
MÁS VIEJOS (por fecha en el nombre de archivo, no por mtime) hasta que el
disco local tenga al menos MIN_FREE_GB libres.

Se creó el 2026-08-07 tras quedarnos sin espacio (100% lleno) tras una falla
del NAS que iba a ser el repositorio principal — mientras el NAS no sea
confiable, esta rutina evita que vuelva a pasar, corriendo periódicamente vía
systemd timer (ver cleanup-video.timer).

Uso manual:
    python3 cleanup_video.py                 # aplica de verdad
    python3 cleanup_video.py --dry-run        # solo muestra qué borraría
"""
import argparse
import os
import re
import shutil
import sys
from pathlib import Path

VIDEO_DIR   = Path(os.environ.get("TRANSCRIBER_VIDEO_DIR", "/home/transcriber/transcriber-linux/output_video"))
MIN_FREE_GB = float(os.environ.get("CLEANUP_MIN_FREE_GB", "150"))
_NAME_RE    = re.compile(r"_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})\.(?:ts|mp4|mkv)$")


def _sort_key(path: Path):
    m = _NAME_RE.search(path.name)
    return m.group(1) + m.group(2) + m.group(3) if m else "9999999999"  # sin fecha -> al final, nunca se borra primero


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not VIDEO_DIR.exists():
        print(f"No existe {VIDEO_DIR}, nada que hacer.")
        return

    free_now = free_gb(VIDEO_DIR)
    print(f"Libre: {free_now:.1f}GB (objetivo: {MIN_FREE_GB:.0f}GB)")
    if free_now >= MIN_FREE_GB:
        print("Ya hay margen suficiente, no se borra nada.")
        return

    segments = sorted(
        [*VIDEO_DIR.glob("canal_*/*.ts"), *VIDEO_DIR.glob("canal_*/*.mp4"),
         *VIDEO_DIR.glob("canal_*/*.mkv")],
        key=_sort_key,
    )
    freed = 0.0
    deleted = 0
    for seg in segments:
        # dry-run no borra de verdad, así que free_gb() nunca cambia -> hay que
        # sumarle lo "borrado" a mano para simular. En modo real, free_gb() YA
        # refleja cada borrado real -- sumar freed otra vez lo contaría dos
        # veces y cortaría la limpieza antes de llegar al objetivo.
        current_free = free_gb(VIDEO_DIR) + (freed / 1e9 if args.dry_run else 0)
        if current_free >= MIN_FREE_GB:
            break
        try:
            size = seg.stat().st_size
        except FileNotFoundError:
            continue
        if args.dry_run:
            print(f"[dry-run] borraría {seg} ({size/1e9:.2f}GB)")
        else:
            seg.unlink()
        freed += size
        deleted += 1

    print(f"{'Borraría' if args.dry_run else 'Borrados'} {deleted} archivos, "
          f"{freed/1e9:.1f}GB {'a liberar' if args.dry_run else 'liberados'}.")
    if not args.dry_run:
        print(f"Libre ahora: {free_gb(VIDEO_DIR):.1f}GB")


if __name__ == "__main__":
    main()
