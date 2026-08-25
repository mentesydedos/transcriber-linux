#!/usr/bin/env python3
"""
backup_nas2_transcripts.py — Respalda las transcripciones (.txt/.srt) al NAS
148.201.38.42 (share Transcripciones), mismo formato que backup_nas2.py para
video:

    NAS_ROOT/YYYY-MM-DD/YYYY-MM-DD_HH-MM_HH-MM/canal_NN_Nombre.txt|.srt

output/ local YA está organizado por bloque de 30 min
(YYYY-MM-DD_HH-MM_HH-MM/canal_NN_Nombre.txt) -- aquí solo hace falta
anidarlo bajo una carpeta de fecha extra para igualar el formato de video.

Puro respaldo -- nunca borra ni mueve nada local. NO sobrescribe si el
destino ya existe: este share ya tenía un historial extenso (desde
2026-04-09) de otro proceso al momento de crear este script (2026-08-11) --
no se debe arriesgar pisarlo.

Uso:
    python3 backup_nas2_transcripts.py
"""
import os
import re
import shutil
import sys
from pathlib import Path

OUTPUT_DIR = Path(os.environ.get("TRANSCRIBER_OUTPUT_DIR", "/home/transcriber/transcriber-linux/output"))
NAS_ROOT   = Path(os.environ.get("BACKUP_NAS2_TX_ROOT", "/mnt/nas2-tx"))
STATE_FILE = Path(os.environ.get("BACKUP_NAS2_TX_STATE", "/home/transcriber/transcriber-linux/logs/backup_nas2_tx_done.txt"))
_BLOCK_RE  = re.compile(r'^(\d{4}-\d{2}-\d{2})_\d{2}-\d{2}_\d{2}-\d{2}$')


def _load_done() -> set:
    if STATE_FILE.exists():
        return set(STATE_FILE.read_text().splitlines())
    return set()


def main():
    if not OUTPUT_DIR.exists():
        print(f"No existe {OUTPUT_DIR}")
        return
    if not NAS_ROOT.is_dir():
        print(f"No existe {NAS_ROOT} -- ¿está montado el NAS?", file=sys.stderr)
        sys.exit(1)

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    done = _load_done()
    copied = 0
    already_there = 0
    with open(STATE_FILE, "a") as state_fh:
        for block_folder in sorted(OUTPUT_DIR.iterdir()):
            if not block_folder.is_dir():
                continue
            m = _BLOCK_RE.match(block_folder.name)
            if not m:
                continue
            date_str = m.group(1)
            for src in block_folder.iterdir():
                if not src.is_file():
                    continue
                key = f"{block_folder.name}/{src.name}"
                if key in done:
                    continue
                dest = NAS_ROOT / date_str / block_folder.name / src.name
                if dest.exists():
                    # Ya había algo ahí (historial de otro proceso) -- no
                    # se sobrescribe, solo se marca como resuelto.
                    state_fh.write(key + "\n")
                    already_there += 1
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(dest.suffix + ".tmp")
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

    print(f"Respaldados {copied} nuevos, {already_there} ya existían en el NAS.")


if __name__ == "__main__":
    main()
