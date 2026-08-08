"""
alerts/health_report.py — Reporte diario de salud del sistema: errores,
integridad de transcripción por canal, estado de red y causas probables de
falla. Se guarda uno por día (health_reports en alerts.db) vía un timer
systemd a las 2am; también se puede generar "en vivo" para el día en curso
desde el dashboard.
"""
import json
import re
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR    = Path(__file__).parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
ALERTS_DB   = BASE_DIR / "alerts.db"
TRANS_DB    = BASE_DIR / "transcriptions.db"
LOG_DIR     = BASE_DIR / "logs"
TVHEADEND_HOST = "148.201.38.136"
NAS_HOST       = "148.201.38.38"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS health_reports (
    date         TEXT PRIMARY KEY,
    report_json  TEXT NOT NULL,
    generated_at TEXT DEFAULT (datetime('now','localtime'))
);
"""


def _alerts_conn():
    conn = sqlite3.connect(str(ALERTS_DB), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def _trans_conn():
    conn = sqlite3.connect(str(TRANS_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# ── Lista combinada de canales (TV + radio) ─────────────────────────────────
def _all_channels() -> list[dict]:
    """26 de TV (nombre real desde output_video/) + 25 de radio (desde el
    M3U) — mismo id que usa el pipeline de transcripción en channel_status."""
    from alerts.videowall import list_channels as _tv
    from alerts.radiowall import list_radio_stations as _radio
    out = [{"num": c["num"], "name": c["label"], "kind": "tv"} for c in _tv()]
    out += [{"num": s["num"], "name": s["name"], "kind": "radio"} for s in _radio()]
    out.sort(key=lambda c: c["num"])
    return out


# ── Sección 1: errores ───────────────────────────────────────────────────────
def _tcp_reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _errors_section(date_str: str) -> dict:
    conn = _trans_conn()
    rows = conn.execute(
        "SELECT channel_id, channel_name, reason, COUNT(*) n "
        "FROM failure_log WHERE timestamp LIKE ? "
        "GROUP BY channel_id, reason ORDER BY n DESC",
        (f"{date_str}%",)
    ).fetchall()
    conn.close()

    by_channel: dict[int, dict] = {}
    total = 0
    for r in rows:
        total += r["n"]
        ch = by_channel.setdefault(r["channel_id"], {
            "channel_id": r["channel_id"], "channel_name": r["channel_name"],
            "count": 0, "reasons": [],
        })
        ch["count"] += r["n"]
        ch["reasons"].append({"reason": r["reason"], "n": r["n"]})

    # Log de video_recorder.py: conteo rápido de líneas WARNING de ese día
    # (corrupción, AC3, etc.) — no se parsea línea por línea a detalle, solo
    # el volumen, que ya es indicativo.
    video_warnings = 0
    vlog = LOG_DIR / "video_recorder.log"
    if vlog.exists():
        try:
            out = subprocess.run(
                ["grep", "-c", f"^{date_str}.*WARNING", str(vlog)],
                capture_output=True, text=True, timeout=30)
            video_warnings = int(out.stdout.strip() or 0)
        except Exception:
            pass

    return {
        "total_failure_log": total,
        "by_channel": sorted(by_channel.values(), key=lambda c: -c["count"]),
        "video_recorder_warnings": video_warnings,
    }


# ── Sección 2: integridad de transcripción por canal ─────────────────────────
def _channels_section(date_str: str) -> dict:
    channels = _all_channels()
    conn = _trans_conn()

    counts = {r["channel_id"]: r["n"] for r in conn.execute(
        "SELECT channel_id, COUNT(*) n FROM transcriptions "
        "WHERE timestamp LIKE ? GROUP BY channel_id", (f"{date_str}%",)
    ).fetchall()}
    spans = {r["channel_id"]: (r["first_ts"], r["last_ts"]) for r in conn.execute(
        "SELECT channel_id, MIN(timestamp) first_ts, MAX(timestamp) last_ts "
        "FROM transcriptions WHERE timestamp LIKE ? GROUP BY channel_id",
        (f"{date_str}%",)
    ).fetchall()}
    status = {r["channel_id"]: r for r in conn.execute(
        "SELECT channel_id, heartbeat, error_count, restart_count, last_error "
        "FROM channel_status"
    ).fetchall()}
    conn.close()

    out = []
    for c in channels:
        n = counts.get(c["num"], 0)
        first_ts, last_ts = spans.get(c["num"], (None, None))
        st = status.get(c["num"])
        # Umbral generoso: con CC-first y VAD algunos chunks se saltan aposta,
        # así que "pocas transcripciones" no siempre es una falla -- se marca
        # como sospechoso solo si prácticamente no hubo nada en el día.
        integrity = "ok" if n >= 20 else ("bajo" if n > 0 else "sin_datos")
        out.append({
            "num": c["num"], "name": c["name"], "kind": c["kind"],
            "transcriptions": n, "first_ts": first_ts, "last_ts": last_ts,
            "integrity": integrity,
            "restart_count": st["restart_count"] if st else None,
            "error_count": st["error_count"] if st else None,
            "last_error": st["last_error"] if st else None,
        })
    return {"channels": out,
            "sin_datos": sum(1 for c in out if c["integrity"] == "sin_datos"),
            "bajo": sum(1 for c in out if c["integrity"] == "bajo"),
            "ok": sum(1 for c in out if c["integrity"] == "ok")}


# ── Sección 3: red ────────────────────────────────────────────────────────────
def _network_section() -> dict:
    tvh_ok = _tcp_reachable(TVHEADEND_HOST, 9981)
    nas_ok = _tcp_reachable(NAS_HOST, 445)
    return {
        "tvheadend": {"host": TVHEADEND_HOST, "reachable": tvh_ok},
        "nas": {"host": NAS_HOST, "reachable": nas_ok},
    }


# ── Sección 4: causas probables ───────────────────────────────────────────────
def _probable_causes(errors: dict, channels: dict, network: dict) -> list[str]:
    causes = []
    if not network["tvheadend"]["reachable"]:
        causes.append("TVHeadend (148.201.38.136) inalcanzable en el momento del reporte — "
                       "revisar la fuente de TV, afecta a los 26 canales.")
    if not network["nas"]["reachable"]:
        causes.append("NAS (148.201.38.38) inalcanzable — falla de hardware conocida "
                       "(se ha apagado solo, aparentemente por temperatura); no afecta "
                       "la grabación/transcripción, que corre en disco local.")
    for ch in errors["by_channel"]:
        reasons = ", ".join(r["reason"][:60] for r in ch["reasons"][:2])
        causes.append(f"Canal {ch['channel_id']:02d} ({ch['channel_name']}): "
                       f"{ch['count']} fallas registradas — {reasons}")
    for ch in channels["channels"]:
        if ch["integrity"] == "sin_datos":
            causes.append(f"Canal {ch['num']:02d} ({ch['name']}): sin ninguna transcripción "
                           f"en el día — posible caída total del canal.")
    if not causes:
        causes.append("Sin causas de falla identificadas — el sistema operó con normalidad.")
    return causes


# ── Generación / persistencia ────────────────────────────────────────────────
def generate_report(date_str: str) -> dict:
    errors   = _errors_section(date_str)
    channels = _channels_section(date_str)
    network  = _network_section()
    return {
        "date": date_str,
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "errors": errors,
        "channels": channels,
        "network": network,
        "probable_causes": _probable_causes(errors, channels, network),
    }


def save_report(date_str: str, report: dict) -> None:
    conn = _alerts_conn()
    conn.execute(
        "INSERT OR REPLACE INTO health_reports (date, report_json, generated_at) "
        "VALUES (?, ?, datetime('now','localtime'))",
        (date_str, json.dumps(report, ensure_ascii=False))
    )
    conn.commit()
    conn.close()


def get_saved_report(date_str: str) -> dict | None:
    conn = _alerts_conn()
    row = conn.execute("SELECT report_json FROM health_reports WHERE date=?", (date_str,)).fetchone()
    conn.close()
    return json.loads(row[0]) if row else None


def list_report_dates() -> list[str]:
    conn = _alerts_conn()
    rows = conn.execute("SELECT date FROM health_reports ORDER BY date DESC").fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_or_generate(date_str: str) -> dict:
    """Para 'hoy' (o cualquier día sin reporte guardado aún) genera en vivo
    sin guardar — el guardado definitivo lo hace la rutina de las 2am sobre
    el día YA CERRADO."""
    saved = get_saved_report(date_str)
    if saved is not None:
        return saved
    return generate_report(date_str)


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    report = generate_report(target)
    save_report(target, report)
    print(f"Reporte de {target} guardado — "
          f"{report['errors']['total_failure_log']} fallas, "
          f"{report['channels']['sin_datos']} canales sin datos, "
          f"TVHeadend={'OK' if report['network']['tvheadend']['reachable'] else 'CAIDO'}, "
          f"NAS={'OK' if report['network']['nas']['reachable'] else 'CAIDO'}")
