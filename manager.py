"""
manager.py — Orquestador con transcripción centralizada en GPU
Arquitectura:
  - 1 proceso transcriber: carga modelo UNA VEZ en GPU, procesa cola
  - N procesos worker: capturan audio con FFmpeg, envían a la cola
Sin competencia de CUDA → calidad y velocidad máximas.
"""

import sys
import time
import signal
import logging
import re
import os
import atexit
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from multiprocessing import Process, Queue

import worker
import transcriber as transcriber_mod

# Forzar UTF-8
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ── Config ────────────────────────────────────────────────────────────────────
M3U_FILE      = "TV audio.m3u"
MAX_CHANNELS  = 8
SKIP_CHANNELS = set()           # sin omisiones

# Transcribers: GPU + CPU en paralelo sobre la misma cola
TRANSCRIBER_MODEL      = "small"
GPU_PARALLEL_WORKERS   = 2      # GPU: 2 streams simultáneos (small float16)
CPU_PARALLEL_WORKERS   = 4      # CPU: 4 workers × 6 threads = 24 hilos i9
CPU_THREADS_PER_WORKER = 6

HEALTH_INTERVAL  = 30
HEARTBEAT_LIMIT  = 180
DB_PATH          = "transcriptions.db"

LOG_DIR      = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
STATUS_FILE  = LOG_DIR / "status.json"
FAILURES_LOG = LOG_DIR / "failures.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MANAGER] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "manager.log", encoding='utf-8'),
    ]
)
# Errores siempre visibles en consola aunque el dashboard esté activo
_console = logging.StreamHandler()
_console.setLevel(logging.WARNING)
_console.setFormatter(logging.Formatter("%(asctime)s [MANAGER] %(levelname)s: %(message)s"))
logging.getLogger("manager").addHandler(_console)
logger = logging.getLogger("manager")

# ── Estructuras ───────────────────────────────────────────────────────────────
@dataclass
class Channel:
    id:            int
    name:          str
    url:           str
    process:       Optional[Process] = None
    restarts:      int = 0
    failed:        bool = False
    last_restart:  Optional[datetime] = None   # momento del último reinicio

# ── Parser M3U ────────────────────────────────────────────────────────────────
def parse_m3u(filepath: str) -> list[dict]:
    channels, current_name = [], None
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line == "#EXTM3U":
                continue
            if line.startswith("#EXTINF"):
                m = re.search(r',(.+)$', line)
                current_name = m.group(1).strip() if m else f"Canal_{len(channels)+1}"
            elif not line.startswith("#"):
                channels.append({"name": current_name or f"Canal_{len(channels)+1}", "url": line})
                current_name = None
    return channels

# ── Watchdog helpers ──────────────────────────────────────────────────────────
def get_heartbeats() -> dict[int, str]:
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        rows = conn.execute("SELECT channel_id, heartbeat FROM channel_status").fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows if r[1]}
    except Exception:
        return {}

def record_failure(ch: Channel, reason: str, action: str):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] Canal {ch.id:02d} '{ch.name}' | {reason} | {action}\n"
    try:
        with open(FAILURES_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT OR IGNORE INTO failure_log (channel_id,channel_name,timestamp,reason,action) VALUES (?,?,?,?,?)",
                     (ch.id, ch.name, ts, reason, action))
        conn.execute("UPDATE channel_status SET last_error=?, error_count=error_count+1, restart_count=restart_count+1 WHERE channel_id=?",
                     (f"[{ts}] {reason}", ch.id))
        conn.commit()
        conn.close()
    except Exception:
        pass

def write_status(channels: list[Channel], heartbeats: dict, transcriber_alive: bool):
    now   = datetime.now()
    estado = []
    for ch in channels:
        hb    = heartbeats.get(ch.id)
        hb_age = None
        if hb:
            try:
                hb_age = int((now - datetime.fromisoformat(hb)).total_seconds())
            except Exception:
                pass
        alive = ch.process is not None and ch.process.is_alive()
        estado.append({
            "id": ch.id, "name": ch.name, "status": "running" if alive else "stopped",
            "restarts": ch.restarts, "failed": ch.failed,
            "heartbeat": hb, "hb_age_sec": hb_age,
            "zombie": hb_age is not None and hb_age > HEARTBEAT_LIMIT and alive,
        })
    payload = {
        "updated_at": now.isoformat(sep=" ", timespec="seconds"),
        "transcriber": {"model": TRANSCRIBER_MODEL,
                        "devices": "cuda+cpu", "alive": transcriber_alive},
        "channels": estado,
    }
    try:
        STATUS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

# ── Control de procesos ───────────────────────────────────────────────────────
def start_worker(ch: Channel, audio_queue: Queue) -> Process:
    p = Process(
        target=worker.run_worker,
        args=(ch.id, ch.name, ch.url, audio_queue),
        name=f"worker-{ch.id:02d}",
        daemon=True,
    )
    p.start()
    logger.info(f"[Canal {ch.id:02d}] Worker iniciado PID={p.pid} name='{ch.name}'")
    return p

def start_transcriber(audio_queue: Queue, device: str,
                      parallel_workers: int, cpu_threads: int,
                      name: str = "transcriber") -> Process:
    p = Process(
        target=transcriber_mod.run,
        args=(audio_queue, TRANSCRIBER_MODEL, device, parallel_workers, cpu_threads),
        name=name,
        daemon=True,
    )
    p.start()
    logger.info(f"Transcriber '{name}' PID={p.pid} model={TRANSCRIBER_MODEL} device={device} "
                f"workers={parallel_workers} threads={cpu_threads}")
    return p

def stop_all(channels: list[Channel], transcriber_procs: list):
    logger.info("Deteniendo todos los procesos...")
    todos = [ch.process for ch in channels if ch.process] + \
            [p for p in transcriber_procs if p]
    for p in todos:
        if p.is_alive():
            p.terminate()
    time.sleep(2)
    for p in todos:
        if p.is_alive():
            p.kill()
    for p in todos:
        p.join(timeout=3)
    logger.info("Todos los procesos detenidos.")

# Configs de cada transcriber: (device, parallel_workers, cpu_threads, name)
_TRANSCRIBER_CONFIGS = [
    ("cuda", GPU_PARALLEL_WORKERS, 8,                   "transcriber-gpu"),
    ("cpu",  CPU_PARALLEL_WORKERS, CPU_THREADS_PER_WORKER, "transcriber-cpu"),
]

# ── Health check ──────────────────────────────────────────────────────────────
def health_check(channels: list[Channel], audio_queue: Queue,
                 transcriber_procs: list) -> list:
    heartbeats = get_heartbeats()
    now        = datetime.now()

    # Verificar cada transcriber
    for i, (p, cfg) in enumerate(zip(transcriber_procs, _TRANSCRIBER_CONFIGS)):
        if p and not p.is_alive():
            device, pw, ct, name = cfg
            logger.error(f"Transcriber '{name}' caído — reiniciando...")
            record_failure(Channel(0, name, ""), f"Transcriber '{name}' caído", "Reinicio")
            transcriber_procs[i] = start_transcriber(audio_queue, device, pw, ct, name)

    # Verificar workers
    for ch in channels:
        if ch.failed:
            continue

        alive = ch.process is not None and ch.process.is_alive()

        # Proceso muerto
        if not alive:
            exit_code = ch.process.exitcode if ch.process else "N/A"
            reason = f"Worker terminó (código={exit_code})"
            ch.restarts += 1
            ch.last_restart = datetime.now()
            logger.warning(f"[Canal {ch.id:02d}] {reason} — reinicio #{ch.restarts}")
            record_failure(ch, reason, f"Reinicio #{ch.restarts}")
            time.sleep(2)
            ch.process = start_worker(ch, audio_queue)
            continue

        # Zombie (proceso vivo pero sin heartbeat)
        hb = heartbeats.get(ch.id)
        if hb:
            try:
                hb_age = int((now - datetime.fromisoformat(hb)).total_seconds())
            except Exception:
                hb_age = 0
            if hb_age > HEARTBEAT_LIMIT:
                reason = f"Zombie: heartbeat hace {hb_age}s"
                logger.warning(f"[Canal {ch.id:02d}] {reason} — kill + reinicio")
                record_failure(ch, reason, f"Kill + Reinicio #{ch.restarts + 1}")
                ch.process.kill()
                ch.process.join(timeout=5)
                ch.restarts += 1
                ch.last_restart = datetime.now()
                time.sleep(2)
                ch.process = start_worker(ch, audio_queue)

    all_alive = all(p is not None and p.is_alive() for p in transcriber_procs)
    write_status(channels, heartbeats, all_alive)
    return transcriber_procs

ANSI_CLEAR   = "\033[2J\033[H"
ANSI_BOLD    = "\033[1m"
ANSI_RESET   = "\033[0m"
ANSI_GREEN   = "\033[32m"
ANSI_YELLOW  = "\033[33m"
ANSI_RED     = "\033[31m"
ANSI_CYAN    = "\033[36m"
ANSI_DIM     = "\033[2m"
ANSI_HIDE_CUR = "\033[?25l"
ANSI_SHOW_CUR = "\033[?25h"

def print_dashboard(channels: list[Channel], transcriber_procs: list,
                    started_at: datetime):
    heartbeats = get_heartbeats()
    now        = datetime.now()
    uptime     = int((now - started_at).total_seconds())
    h, m, s    = uptime // 3600, (uptime % 3600) // 60, uptime % 60

    def t_badge(p, label):
        alive  = p is not None and p.is_alive()
        color  = ANSI_GREEN if alive else ANSI_RED
        symbol = "●" if alive else "✖"
        return f"{color}{symbol}{ANSI_RESET} {label}"

    gpu_badge = t_badge(transcriber_procs[0] if transcriber_procs else None, "GPU")
    cpu_badge = t_badge(transcriber_procs[1] if len(transcriber_procs) > 1 else None, "CPU")

    try:
        cols = os.get_terminal_size().columns
    except Exception:
        cols = 100
    cols = max(cols, 80)
    line = "─" * cols

    buf = [ANSI_CLEAR, ANSI_HIDE_CUR]
    buf.append(f"{ANSI_BOLD}{'TRANSCRIBER MANAGER':^{cols}}{ANSI_RESET}\n")
    buf.append(f"{ANSI_DIM}{line}{ANSI_RESET}\n")
    buf.append(
        f"  {ANSI_BOLD}Modelo:{ANSI_RESET} {TRANSCRIBER_MODEL}"
        f"   {gpu_badge}  {cpu_badge}"
        f"   {ANSI_BOLD}Uptime:{ANSI_RESET} {h:02d}:{m:02d}:{s:02d}"
        f"   {ANSI_DIM}{now.strftime('%H:%M:%S')}{ANSI_RESET}\n"
    )
    buf.append(f"{ANSI_DIM}{line}{ANSI_RESET}\n")
    buf.append(
        f"  {'#':>2}  {'Canal':<22}  {'Proceso':<8}  {'Heartbeat':>9}  "
        f"{'Rst':>4}  {'Rst/h':>5}  {'Estabilidad':<14}  {'Último corte'}\n"
    )
    buf.append(f"{ANSI_DIM}{line}{ANSI_RESET}\n")

    uptime_h = max(uptime / 3600, 1 / 60)   # mínimo 1 min para evitar div/0

    for ch in channels:
        alive  = ch.process is not None and ch.process.is_alive()
        hb     = heartbeats.get(ch.id)
        hb_age = None
        if hb:
            try:
                hb_age = int((now - datetime.fromisoformat(hb)).total_seconds())
            except Exception:
                pass

        if ch.failed:
            proc_col = f"{ANSI_RED}FALLIDO {ANSI_RESET}"
        elif alive:
            proc_col = f"{ANSI_GREEN}running {ANSI_RESET}"
        else:
            proc_col = f"{ANSI_RED}MUERTO  {ANSI_RESET}"

        if hb_age is None:
            hb_col = f"{ANSI_DIM}  sin HB {ANSI_RESET}"
        elif hb_age > HEARTBEAT_LIMIT:
            hb_col = f"{ANSI_RED}ZMB{hb_age:>3}s{ANSI_RESET}"
        elif hb_age > 60:
            hb_col = f"{ANSI_YELLOW}{hb_age:>6}s {ANSI_RESET}"
        else:
            hb_col = f"{ANSI_GREEN}{hb_age:>6}s {ANSI_RESET}"

        # Índice de estabilidad
        rst_h = ch.restarts / uptime_h
        if rst_h == 0:
            stab_color = ANSI_GREEN
            stab_bar   = "▓▓▓▓▓▓▓▓▓▓"
            stab_pct   = "100%"
        elif rst_h < 2:
            stab_color = ANSI_YELLOW
            fill = max(1, round((1 - rst_h / 6) * 10))
            stab_bar   = "▓" * fill + "░" * (10 - fill)
            stab_pct   = f"{max(0, 100 - int(rst_h/6*100)):>3}%"
        else:
            stab_color = ANSI_RED
            fill = max(0, round((1 - min(rst_h, 6) / 6) * 10))
            stab_bar   = "▓" * fill + "░" * (10 - fill)
            stab_pct   = f"{max(0, 100 - int(min(rst_h,6)/6*100)):>3}%"

        rst_h_str  = f"{ANSI_DIM}{rst_h:>5.1f}{ANSI_RESET}"
        rst_col    = f"{ANSI_YELLOW}{ch.restarts:>4}{ANSI_RESET}" if ch.restarts > 0 \
                     else f"{ANSI_DIM}{ch.restarts:>4}{ANSI_RESET}"
        stab_col   = f"{stab_color}{stab_bar} {stab_pct}{ANSI_RESET}"

        if ch.last_restart:
            ago = int((now - ch.last_restart).total_seconds())
            lr_str = f"{ago//60}m{ago%60:02d}s atrás"
        else:
            lr_str = f"{ANSI_DIM}sin cortes{ANSI_RESET}"

        nombre = ch.name[:22]
        buf.append(
            f"  {ch.id:>2}  {nombre:<22}  {proc_col}  {hb_col}  "
            f"{rst_col}  {rst_h_str}  {stab_col}  {lr_str}\n"
        )

    buf.append(f"{ANSI_DIM}{line}{ANSI_RESET}\n")
    workers_ok = sum(1 for ch in channels if ch.process and ch.process.is_alive())
    buf.append(
        f"  {ANSI_DIM}Canales activos: {workers_ok}/{len(channels)}"
        f"  |  Ver transcripciones: python monitor.py{ANSI_RESET}\n"
    )

    sys.stdout.write("".join(buf))
    sys.stdout.flush()

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    if not Path(M3U_FILE).exists():
        logger.error(f"No se encontró '{M3U_FILE}'.")
        sys.exit(1)

    raw = parse_m3u(M3U_FILE)
    if not raw:
        logger.error("El M3U no contiene canales válidos.")
        sys.exit(1)

    raw = [ch for i, ch in enumerate(raw[:MAX_CHANNELS], start=1)
           if i not in SKIP_CHANNELS]
    logger.info(f"Canales activos: {len(raw)} (omitidos: {sorted(SKIP_CHANNELS)})")

    channels = [Channel(id=i+1, name=ch["name"], url=ch["url"])
                for i, ch in enumerate(raw)]

    # Cola compartida — GPU y CPU consumen de la misma cola (load balancing automático)
    audio_queue = Queue(maxsize=64)

    transcriber_procs = [None, None]
    atexit.register(stop_all, channels, transcriber_procs)

    # Iniciar transcribers (GPU primero, luego CPU)
    logger.info(f"Iniciando transcriber GPU ({TRANSCRIBER_MODEL} cuda, {GPU_PARALLEL_WORKERS} workers)...")
    transcriber_procs[0] = start_transcriber(
        audio_queue, "cuda", GPU_PARALLEL_WORKERS, 8, "transcriber-gpu")

    logger.info(f"Iniciando transcriber CPU ({TRANSCRIBER_MODEL} cpu, {CPU_PARALLEL_WORKERS} workers × {CPU_THREADS_PER_WORKER} threads)...")
    transcriber_procs[1] = start_transcriber(
        audio_queue, "cpu", CPU_PARALLEL_WORKERS, CPU_THREADS_PER_WORKER, "transcriber-cpu")

    logger.info("Esperando ~35s mientras cargan los modelos...")
    time.sleep(35)

    # Iniciar workers con delay escalonado
    logger.info("Iniciando workers de audio...")
    for ch in channels:
        ch.process = start_worker(ch, audio_queue)
        time.sleep(4)

    started_at = datetime.now()
    logger.info(f"Sistema activo: {len(channels)} canales → {TRANSCRIBER_MODEL} GPU+CPU")

    def handle_exit(*_):
        sys.stdout.write(ANSI_SHOW_CUR + "\n")
        sys.stdout.flush()
        stop_all(channels, transcriber_procs)
        os._exit(0)

    signal.signal(signal.SIGINT,  handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    # Dashboard inicial
    print_dashboard(channels, transcriber_procs, started_at)

    # Loop de supervisión — refresca dashboard cada HEALTH_INTERVAL segundos
    while True:
        time.sleep(HEALTH_INTERVAL)
        transcriber_procs = health_check(channels, audio_queue, transcriber_procs)
        print_dashboard(channels, transcriber_procs, started_at)


if __name__ == "__main__":
    main()
