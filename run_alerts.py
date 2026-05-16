#!/usr/bin/env python3
"""
run_alerts.py — Arranca el dashboard de alertas AlertaTV.
Uso:
  source venv/bin/activate
  python run_alerts.py
  python run_alerts.py --port 5001
"""
import sys
import logging
import argparse
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
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5001)
    parser.add_argument('--host', default='0.0.0.0')
    args = parser.parse_args()

    app = create_app()
    start_watcher()

    print(f"\n  AlertaTV corriendo en  http://localhost:{args.port}")
    print(f"  Ctrl+C para detener\n")

    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
