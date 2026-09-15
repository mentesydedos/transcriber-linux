#!/usr/bin/env python3
"""
service_watchdog.py — Corre run_full_health_check() (system_health.py,
100% de solo lectura) cada 10-15 min vía systemd timer, y:

  1) Alerta por Telegram SOLO cuando el estado de un servicio CAMBIA (recién
     caído / recién recuperado) -- no en cada corrida mientras sigue igual,
     para no saturar de mensajes repetidos. El último estado conocido se
     guarda en la tabla settings de alerts.db (misma tabla que ya usa
     _get_setting/_set_setting en alerts/watcher.py).
  2) Guarda cada resultado por servicio en service_health_log (alerts.db)
     -- historial ligero para una futura vista de "disponibilidad en el
     tiempo por servicio", que hoy no existe (health_reports es diario y
     por CANAL, no por servicio; system_health.py no guarda historial).
     Poda filas más viejas que RETENTION_DAYS al final de cada corrida.

Reusa send_telegram() (alerts/telegram.py) directo con el tg_token/tg_chat_id
globales ya guardados en settings -- NO credenciales nuevas.

Uso:
    python3 service_watchdog.py
"""
import html
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
ALERTS_DB = BASE_DIR / "alerts.db"

from system_health import run_full_health_check
from alerts.telegram import send_telegram

RETENTION_DAYS = 60
STATE_SETTING_KEY = "watchdog_last_state"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS service_health_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT    DEFAULT (datetime('now','localtime')),
    service    TEXT    NOT NULL,
    ok         INTEGER NOT NULL,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_shl_service_time ON service_health_log(service, checked_at);
"""

_TUNE_PRAGMAS = (
    "PRAGMA synchronous=NORMAL",
    "PRAGMA cache_size=-64000",
)


def _alerts_conn():
    conn = sqlite3.connect(str(ALERTS_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    for p in _TUNE_PRAGMAS:
        conn.execute(p)
    conn.executescript(_SCHEMA)
    return conn


def _get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _set_setting(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))


def _log_and_prune(conn, steps):
    for s in steps:
        conn.execute(
            "INSERT INTO service_health_log (service, ok, detail) VALUES (?,?,?)",
            (s["name"], int(s["ok"]), s["detail"]),
        )
    cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM service_health_log WHERE checked_at < ?", (cutoff,))
    conn.commit()


def _diff_and_alert(conn, steps):
    old_state = json.loads(_get_setting(conn, STATE_SETTING_KEY, "{}") or "{}")
    new_state = {s["name"]: s["ok"] for s in steps}

    down_now, recovered = [], []
    for s in steps:
        old_ok = old_state.get(s["name"])
        if not s["ok"] and old_ok is not False:
            down_now.append(s)
        elif s["ok"] and old_ok is False:
            recovered.append(s)

    _set_setting(conn, STATE_SETTING_KEY, json.dumps(new_state))
    conn.commit()

    if not (down_now or recovered):
        return

    cfg = {r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM settings")}
    token, chat_id = cfg.get("tg_token", ""), cfg.get("tg_chat_id", "")
    if not token or not chat_id:
        print("Sin tg_token/tg_chat_id en settings -- no se manda alerta (solo se registra).", file=sys.stderr)
        return

    lines = ["<b>AlertaTV — cambio de estado de servicios</b>"]
    for s in down_now:
        lines.append(f"\U0001f534 <b>{html.escape(s['name'])}</b>: CAÍDO — {html.escape(s['detail'])}")
    for s in recovered:
        lines.append(f"\U0001f7e2 <b>{html.escape(s['name'])}</b>: recuperado")
    ok, err = send_telegram(token, chat_id, "\n".join(lines))
    if not ok:
        print(f"Fallo enviando alerta Telegram: {err}", file=sys.stderr)


def main():
    status = run_full_health_check()   # ya escribe logs/health_check_status.json
    conn = _alerts_conn()
    try:
        _diff_and_alert(conn, status["steps"])
        _log_and_prune(conn, status["steps"])
    finally:
        conn.close()
    ok_n = sum(1 for s in status["steps"] if s["ok"])
    print(f"Vigilante: {ok_n}/{len(status['steps'])} servicios OK.")


if __name__ == "__main__":
    main()
