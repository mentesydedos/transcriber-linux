#!/usr/bin/env python3
"""
backup_nas2_radio.py — Sube al NAS 148.201.38.42 los bloques de audio de
radio ya cerrados (ver radio_recorder.py) y los BORRA localmente en cuanto
ya quedaron ahí -- a diferencia de backup_nas2.py (video), aquí el disco
local es solo un buffer de paso, no un respaldo adicional: el NAS es el
único almacén, con retención fija de 30 días (ver cleanup_nas_radio.py).

    NAS_ROOT/YYYY-MM-DD/YYYY-MM-DD_HH-MM_HH-MM/canal_NN_Nombre_..._HH-MM.aac

Un bloque se considera "cerrado" si NO es el más reciente de su carpeta
(radio_recorder.py sigue escribiendo el último con -segment_time hasta que
rota) -- mismo criterio simple que usa backup_nas2.py implícitamente al
solo tocar bloques ya con nombre fijo, pero aquí se hace explícito porque
además se borra: subir+borrar el bloque que ffmpeg todavía tiene abierto
cortaría la grabación en curso.

Uso:
    python3 backup_nas2_radio.py
"""
import os
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

AUDIO_DIR  = Path(os.environ.get("TRANSCRIBER_RADIO_DIR", "/home/transcriber/transcriber-linux/output_radio"))
NAS_ROOT   = Path(os.environ.get("BACKUP_NAS2_RADIO_ROOT", "/mnt/nas2-tv/audio_radio"))
STATE_FILE = Path(os.environ.get("BACKUP_NAS2_RADIO_STATE", "/home/transcriber/transcriber-linux/logs/backup_nas2_radio_done.txt"))
_SEG_RE    = re.compile(r'_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})\.aac$')


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
    if not AUDIO_DIR.exists():
        print(f"No existe {AUDIO_DIR}")
        return
    if not NAS_ROOT.parent.is_dir():
        print(f"No existe {NAS_ROOT.parent} -- ¿está montado el NAS?", file=sys.stderr)
        sys.exit(1)
    NAS_ROOT.mkdir(parents=True, exist_ok=True)

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    done = _load_done()
    copied = 0
    with open(STATE_FILE, "a") as state_fh:
        for folder in sorted(AUDIO_DIR.iterdir()):
            if not folder.is_dir() or not folder.name.startswith("canal_"):
                continue
            blocks = sorted(folder.glob("*.aac"))
            if not blocks:
                continue
            # El último bloque de la carpeta puede seguir abierto -- se deja
            # para la siguiente corrida (rota cada 30 min, esta corre cada
            # minuto, así que como mucho tarda un ciclo extra en subirse).
            for src in blocks[:-1]:
                key = f"{folder.name}/{src.name}"
                if key in done:
                    src.unlink(missing_ok=True)  # ya se había subido antes; solo faltaba borrar
                    src.with_suffix(".driftlog").unlink(missing_ok=True)
                    continue
                dest = _dest_for(src)
                if dest is None:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(".aac.tmp")
                try:
                    shutil.copy2(src, tmp)
                    tmp.replace(dest)
                except Exception as e:
                    print(f"  falló copiando {key}: {e}", file=sys.stderr)
                    tmp.unlink(missing_ok=True)
                    continue
                state_fh.write(key + "\n")
                state_fh.flush()
                src.unlink(missing_ok=True)
                # El sidecar .driftlog (ver radio_recorder.py) no se sube al
                # NAS -- solo sirve para ubicar clips de audio con precisión
                # mientras el bloque está local/reciente. Para bloques viejos
                # ya archivados, audio_clips.py cae al cálculo lineal simple.
                src.with_suffix(".driftlog").unlink(missing_ok=True)
                copied += 1

    print(f"Respaldados {copied} bloques nuevos al NAS (y borrados localmente).")


if __name__ == "__main__":
    main()
