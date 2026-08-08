#!/usr/bin/env python3
"""
run_watcher.py — Proceso standalone del watcher de alertas (correos/Telegram).

Separado del dashboard web (wsgi.py / alerts.service) a propósito: Gunicorn
corre varios workers como procesos independientes, y start_watcher() lanza
un hilo de fondo -- si viviera dentro de wsgi.py, cada worker lanzaría su
propia copia y cada alerta se enviaría N veces. Este proceso es la única
copia del watcher, sin importar cuántos workers web haya.

Uso:
  source venv/bin/activate
  python run_watcher.py
"""
import sys
import time
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

from alerts.app     import create_app
from alerts.watcher import start_watcher

if __name__ == '__main__':
    # create_app() solo por su efecto secundario (_init_db + esquema EPG),
    # para garantizar que las tablas existan aunque este proceso arranque
    # antes que Gunicorn. El objeto Flask no se usa -- este proceso nunca
    # sirve HTTP.
    create_app()
    start_watcher()

    print("\n  Watcher de alertas AlertaTV corriendo (sin servir HTTP)")
    print("  Ctrl+C para detener\n")

    # start_watcher() lanza un hilo daemon -- sin este loop el proceso
    # terminaría de inmediato y el hilo moriría con él.
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
