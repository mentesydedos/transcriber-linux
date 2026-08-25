#!/usr/bin/env python3
"""
backup_nas2.py — Respalda los bloques de video ya finalizados (.mp4, ver
finalize_video.py) al NAS 148.201.38.42, REORGANIZADOS por fecha y bloque de
30 min (no por canal como en local):

    NAS_ROOT/YYYY-MM-DD/YYYY-MM-DD_HH-MM_HH-MM/canal_NN_Nombre_..._HH-MM.mp4

Puro respaldo -- NUNCA borra ni mueve nada local, solo copia. cleanup_video.py
sigue siendo el único que libera espacio en disco local; este script debe
mantenerse al corriente ANTES de que cleanup_video.py borre algo, por eso
corre cada minuto (igual que finalize_video.py) en vez de una vez al día.

Solo toca .mp4 ya finalizados (bloques cerrados, nunca a medio escribir) --
los .ts/.mkv activos se ignoran, finalize_video.py los convierte cuando
cierran.

Estado de qué ya se respaldó: un archivo de marcadores local (evita
preguntarle al NAS por CIFS por cada uno de miles de bloques históricos en
cada corrida, que sería lento).

Uso:
    python3 backup_nas2.py
"""
import os
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

VIDEO_DIR  = Path(os.environ.get("TRANSCRIBER_VIDEO_DIR", "/home/transcriber/transcriber-linux/output_video"))
NAS_ROOT   = Path(os.environ.get("BACKUP_NAS2_ROOT", "/mnt/nas2-tv/videos 720x480"))
STATE_FILE = Path(os.environ.get("BACKUP_NAS2_STATE", "/home/transcriber/transcriber-linux/logs/backup_nas2_done.txt"))
_SEG_RE    = re.compile(r'_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})\.mp4$')


def _load_done() -> set:
    if STATE_FILE.exists():
        return set(STATE_FILE.read_text().splitlines())
    return set()


def _dest_for(path: Path) -> Path | None:
    m = _SEG_RE.search(path.name)
    if not m:
        return None
    date_str, hh, mm = m.groups()
    start = datetime.strptime(f"{date_str} {hh}:{mm}", "%Y-%m-%d %H:%M")
    end = start + timedelta(minutes=30)
    block = f"{date_str}_{hh}-{mm}_{end.strftime('%H-%M')}"
    return NAS_ROOT / date_str / block / path.name


def main():
    if not VIDEO_DIR.exists():
        print(f"No existe {VIDEO_DIR}")
        return
    if not NAS_ROOT.is_dir():
        print(f"No existe {NAS_ROOT} -- ¿está montado el NAS?", file=sys.stderr)
        sys.exit(1)

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    done = _load_done()
    copied = 0
    with open(STATE_FILE, "a") as state_fh:
        for folder in sorted(VIDEO_DIR.iterdir()):
            if not folder.is_dir() or not folder.name.startswith("canal_"):
                continue
            for src in sorted(folder.glob("*.mp4")):
                key = f"{folder.name}/{src.name}"
                if key in done:
                    continue
                dest = _dest_for(src)
                if dest is None:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(".mp4.tmp")
                try:
                    shutil.copy2(src, tmp)
                    tmp.replace(dest)
                except Exception as e:
                    print(f"  falló copiando {key}: {e}", file=sys.stderr)
                    tmp.unlink(missing_ok=True)
                    continue
                state_fh.write(key + "\n")
                state_fh.flush()
                copied += 1

    print(f"Respaldados {copied} bloques nuevos.")


if __name__ == "__main__":
    main()
