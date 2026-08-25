#!/usr/bin/env python3
"""
cleanup_nas_radio.py — Borra del NAS las carpetas de audio de radio (ver
backup_nas2_radio.py) con más de RETENTION_DAYS días de antigüedad. A
diferencia de cleanup_video.py (que borra por presión de espacio en disco
LOCAL), esto es retención por FECHA FIJA en el NAS -- ahí sobra espacio
(25TB libres al momento de escribir esto), lo que se quiere es una ventana
móvil de 30 días, ni más ni menos, por pedido explícito.

Corre una vez al día vía systemd timer (ver cleanup-nas-radio.timer).

Uso manual:
    python3 cleanup_nas_radio.py                 # aplica de verdad
    python3 cleanup_nas_radio.py --dry-run        # solo muestra qué borraría
"""
import argparse
import os
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

NAS_ROOT       = Path(os.environ.get("BACKUP_NAS2_RADIO_ROOT", "/mnt/nas2-tv/audio_radio"))
RETENTION_DAYS = int(os.environ.get("RADIO_RETENTION_DAYS", "30"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not NAS_ROOT.is_dir():
        print(f"No existe {NAS_ROOT} -- ¿está montado el NAS?", file=sys.stderr)
        sys.exit(1)

    cutoff = date.today() - timedelta(days=RETENTION_DAYS)
    print(f"Hoy: {date.today().isoformat()} -- borrando carpetas de fecha < {cutoff.isoformat()} "
          f"(retención {RETENTION_DAYS} días)")

    deleted = 0
    for day_dir in sorted(NAS_ROOT.iterdir()):
        if not day_dir.is_dir():
            continue
        try:
            day = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
        except ValueError:
            continue  # carpeta que no sigue el patrón de fecha -- no se toca
        if day < cutoff:
            if args.dry_run:
                print(f"[dry-run] borraría {day_dir}")
            else:
                shutil.rmtree(day_dir, ignore_errors=True)
                print(f"Borrado {day_dir}")
            deleted += 1

    print(f"{'Borraría' if args.dry_run else 'Borradas'} {deleted} carpetas de día.")


if __name__ == "__main__":
    main()
