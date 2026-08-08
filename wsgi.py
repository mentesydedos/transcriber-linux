#!/usr/bin/env python3
"""
wsgi.py — Entrypoint para Gunicorn del dashboard AlertaTV.
Uso:
  venv/bin/gunicorn --workers 4 --threads 6 --bind 127.0.0.1:8001 --timeout 0 wsgi:app

NO arranca el watcher de alertas (correos/Telegram) -- eso corre aparte en
run_watcher.py / alerts-watcher.service, exactamente una vez, sin importar
cuántos workers de Gunicorn haya. Ver run_watcher.py.
"""
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

Path("logs").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/alerts.log", encoding="utf-8"),
    ]
)

from alerts.app import create_app

app = create_app()
