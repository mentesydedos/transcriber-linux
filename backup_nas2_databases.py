#!/usr/bin/env python3
"""
backup_nas2_databases.py — Respalda alerts.db y transcriptions.db al NAS
148.201.38.42, usando la API de respaldo online de SQLite (Connection.backup),
NO una copia de archivo cruda -- ambas bases están en modo WAL y se escriben
en vivo (8 workers de gunicorn + el watcher), una copia cruda podría capturar
un .db a medio escribir e inconsistente con su -wal.

Dos pasos: (1) respaldo local rápido (misma máquina, sin latencia de red
durante la ventana en que se está "persiguiendo" a los escritores activos);
(2) copia del snapshot local YA CONSISTENTE al NAS con el mismo patrón
.tmp + Path.replace() que usan backup_nas2.py/backup_nas2_radio.py/
backup_nas2_transcripts.py.

Retención fija de RETENTION_DAYS días en el NAS (poda al final de esta misma
corrida -- a diferencia de video/radio, aquí no hace falta un cleanup_*.py
aparte: esto ya corre una vez al día).

Uso:
    python3 backup_nas2_databases.py
"""
import os
import shutil
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

BASE_DIR   = Path(__file__).parent
ALERTS_DB  = Path(os.environ.get("TRANSCRIBER_ALERTS_DB", BASE_DIR / "alerts.db"))
TRANS_DB   = Path(os.environ.get("TRANSCRIBER_TRANS_DB", BASE_DIR / "transcriptions.db"))
NAS_ROOT   = Path(os.environ.get("BACKUP_NAS2_DB_ROOT", "/mnt/nas2-tv/db_backups"))
STAGE_DIR  = Path(os.environ.get("BACKUP_NAS2_DB_STAGE", BASE_DIR / "logs" / "db_backup_staging"))
RETENTION_DAYS = int(os.environ.get("BACKUP_NAS2_DB_RETENTION_DAYS", "30"))


def _local_backup(src: Path, tmp_local: Path) -> None:
    tmp_local.unlink(missing_ok=True)
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(tmp_local))
    try:
        # pages=1000/sleep=0.05: copia en pasos, cede el paso entre cada uno
        # -- evita mantener el lock de respaldo abierto de un tirón sobre
        # una base de varios GB mientras 8 workers de gunicorn + el watcher
        # siguen escribiendo.
        src_conn.backup(dst_conn, pages=1000, sleep=0.05)
    finally:
        dst_conn.close()
        src_conn.close()


def _push_to_nas(local_path: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        shutil.copy2(local_path, tmp)
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _prune_old(nas_root: Path, retention_days: int) -> None:
    cutoff = date.today() - timedelta(days=retention_days)
    if not nas_root.is_dir():
        return
    for day_dir in sorted(nas_root.iterdir()):
        if not day_dir.is_dir():
            continue
        try:
            day = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            shutil.rmtree(day_dir, ignore_errors=True)
            print(f"Borrado respaldo antiguo {day_dir}")


def main():
    if not NAS_ROOT.parent.is_dir():
        print(f"No existe {NAS_ROOT.parent} -- ¿está montado el NAS?", file=sys.stderr)
        sys.exit(1)
    NAS_ROOT.mkdir(parents=True, exist_ok=True)
    STAGE_DIR.mkdir(parents=True, exist_ok=True)

    today = date.today().isoformat()
    ok_count = 0
    for name, src in [("alerts.db", ALERTS_DB), ("transcriptions.db", TRANS_DB)]:
        if not src.exists():
            print(f"No existe {src}", file=sys.stderr)
            continue
        local_tmp = STAGE_DIR / f"{name}.tmp"
        dest = NAS_ROOT / today / name
        t0 = time.time()
        try:
            _local_backup(src, local_tmp)
            _push_to_nas(local_tmp, dest)
            size_mb = dest.stat().st_size / 1e6
            print(f"Respaldado {name} -> {dest} ({time.time()-t0:.1f}s, {size_mb:.1f} MB)")
            ok_count += 1
        except Exception as e:
            print(f"  falló respaldando {name}: {e}", file=sys.stderr)
        finally:
            local_tmp.unlink(missing_ok=True)

    _prune_old(NAS_ROOT, RETENTION_DAYS)
    print(f"Listo: {ok_count}/2 bases respaldadas.")


if __name__ == "__main__":
    main()
