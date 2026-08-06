"""
manager.py — Orquestador del sistema de transcripción TV (Linux + Qwen3-ASR)

Arquitectura híbrida GPU + CPU:
  - 1 proceso worker FFmpeg por canal (N canales)
      → cada uno con su PROPIA cola de salida (maxsize=3, drop-oldest)
  - Pool heterogéneo de inference workers:
      · 1× GPU (Qwen-1.7B fp16, batch=4)
      · M× CPU (Qwen-1.7B fp32, batch=1, 8 hilos c/u)
  - Thread dispatcher en el manager: recorre las colas por canal y empuja
    el chunk del canal MÁS ATRASADO a la jobs_queue global. Los inference
    workers consumen esa cola (work-stealing natural).

Garantías:
  - Un canal roto solo llena su cola → drop-oldest → no bloquea a los demás.
  - Si un inference worker cae, los otros siguen; el manager lo reinicia.
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
import queue as _queue
import threading
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from multiprocessing import Process, Queue, Event as MPEvent

import worker

# Motor de inferencia seleccionable por env: 'qwen' (default, transcriber.py) o
# 'parakeet' (transcriber_parakeet.py, requiere venv-parakeet). Import perezoso:
# importar el módulo del motor no usado fallaría porque sus dependencias
# (qwen_asr vs nemo) no coexisten en el mismo venv.
TRANSCRIBER_ENGINE = os.environ.get("TRANSCRIBER_ENGINE", "qwen").lower()
if TRANSCRIBER_ENGINE == "parakeet":
    import transcriber_parakeet as transcriber_mod
elif TRANSCRIBER_ENGINE == "cohere":
    import transcriber_cohere as transcriber_mod
else:
    import transcriber as transcriber_mod

# Forzar UTF-8
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ── Config ────────────────────────────────────────────────────────────────────
# Fuente configurable por env: el cutover a la tarjeta local apunta a local.m3u
# (servido por el sintonizador). Rollback rápido: volver a "TV audio.m3u".
M3U_FILE          = os.environ.get("TRANSCRIBER_M3U", "TV audio.m3u")
MAX_CHANNELS      = int(os.environ.get("TRANSCRIBER_MAX_CHANNELS", "8"))
SKIP_CHANNELS     = set()

TRANSCRIBER_MODEL = ("nvidia/parakeet-tdt-0.6b-v3" if TRANSCRIBER_ENGINE == "parakeet"
                     else "CohereLabs/cohere-transcribe-03-2026" if TRANSCRIBER_ENGINE == "cohere"
                     else "Qwen/Qwen3-ASR-0.6B")

# Split de motores por canal: dos instancias de manager.py (una por venv/engine)
# pueden correr a la vez sobre el mismo M3U, cada una cubriendo un subconjunto de
# canales por NOMBRE. Pensado para el híbrido Cohere (noticias, fuerza idioma) +
# Parakeet (resto, mucho más rápido) — ver transcriber-integracion-parakeet.
def _parse_name_list(raw: str) -> set:
    return {n.strip() for n in raw.split(",") if n.strip()}

ONLY_CHANNELS = _parse_name_list(os.environ.get("TRANSCRIBER_ONLY_CHANNELS", ""))
SKIP_CHANNEL_NAMES = _parse_name_list(os.environ.get("TRANSCRIBER_SKIP_CHANNEL_NAMES", ""))

# Pool de inference workers. Cada entrada: (name, device, threads, weight, queue_maxsize)
#   weight = cuántos chunks se le asignan por ronda (round-robin ponderado).
#   Relación weights aproximada a la capacidad relativa medida en prod:
#     GPU ~5.3× rt, CPU ~1.9× rt → ratio ~2.8:1, redondeado a 2:1 para dejar
#     al CPU llevar un poco más de carga y bajar utilización del GPU.
if TRANSCRIBER_ENGINE == "parakeet":
    # Parakeet-TDT-0.6B ~51× rt en la T1000 → 1 solo worker GPU basta para los
    # 8 canales con enorme margen. NeMo en CPU sería lento y complejo: no se usa.
    INFERENCE_POOL = [
        # (name, device, threads, weight, maxsize)
        ("gpu", "cuda", None, 1, 12),
    ]
elif TRANSCRIBER_ENGINE == "cohere":
    # Cohere Transcribe ~15× rt medido en A/B (T1000, más lento que Parakeet por
    # ser AED 2B vs RNNT 0.6B) — pensado para servir solo 2-3 canales de noticias
    # (ver TRANSCRIBER_ONLY_CHANNELS), así que 1 worker GPU sobra igual. CPU
    # inviable: un AED de 2B en CPU sería demasiado lento para tiempo real.
    INFERENCE_POOL = [
        ("gpu", "cuda", None, 1, 12),
    ]
else:
    # Config óptima medida para Qwen-0.6B en este hardware (T1000 + i9-14900):
    # 1 GPU + 1 CPU con 16 hilos → 15.9× rt, 0 drops. Agregar un 2° CPU no ayuda
    # porque la contención de hilos entre workers reduce el throughput total
    # (probado: 3 workers = 5.65× rt, peor que 2). La GPU al 98-100% es
    # esperado y correcto a este throughput.
    INFERENCE_POOL = [
        ("gpu",    "cuda", None,    2,      12),
        ("cpu-1",  "cpu",  16,      1,      6),
    ]

CHANNEL_QUEUE_MAXSIZE = 3          # drop-oldest por canal (3 × 30s = 90s buffer)
DISPATCHER_POLL_MS    = 50         # granularidad del dispatcher

HEALTH_INTERVAL   = 30
HEARTBEAT_LIMIT   = 180
DB_PATH           = os.environ.get("TRANSCRIBER_DB", "transcriptions.db")

LOG_DIR           = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
STATUS_SUFFIX     = f"-{TRANSCRIBER_ENGINE}" if TRANSCRIBER_ENGINE == "cohere" else ""
STATUS_FILE       = LOG_DIR / f"status{STATUS_SUFFIX}.json"
FAILURES_LOG      = LOG_DIR / "failures.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MANAGER] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "manager.log", encoding='utf-8'),
    ]
)
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
    headers:       Optional[str]     = None      # ver #EXTHEADER en parse_m3u
    process:       Optional[Process] = None
    out_queue:     Optional[Queue]   = None      # mp.Queue(maxsize=3) exclusiva del canal
    restarts:      int = 0
    failed:        bool = False
    last_restart:  Optional[datetime] = None

@dataclass
class InferenceWorker:
    name:          str
    device:        str                           # 'cuda' | 'cpu'
    threads:       Optional[int]
    weight:        int              = 1          # peso en el round-robin del dispatcher
    maxsize:       int              = 6          # tamaño de su cola de entrada
    in_queue:      Optional[Queue]  = None       # cola exclusiva del worker
    assigned:      int              = 0          # chunks asignados (para RR ponderado)
    process:       Optional[Process] = None
    ready_event:   object            = None      # mp.Event
    restarts:      int                = 0
    last_restart:  Optional[datetime] = None

# ── Parser M3U ────────────────────────────────────────────────────────────────
def parse_m3u(filepath: str) -> list[dict]:
    """#EXTHEADER (opcional, entre #EXTINF y la URL) manda un header HTTP crudo
    en la conexión de ffmpeg/ffprobe a esa URL — necesario para estaciones de
    radio detrás de Zeno.fm, que rechazan con 401 sin un Origin/Referer del
    sitio autorizado. Ejemplo:
        #EXTINF:-1,Nombre
        #EXTHEADER:Origin: https://sitio-dueno.com
        https://stream.zeno.fm/xxxxx
    """
    channels, current_name, current_headers = [], None, None
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line == "#EXTM3U":
                continue
            if line.startswith("#EXTINF"):
                m = re.search(r',(.+)$', line)
                current_name = m.group(1).strip() if m else f"Canal_{len(channels)+1}"
            elif line.startswith("#EXTHEADER:"):
                current_headers = line[len("#EXTHEADER:"):].strip()
            elif not line.startswith("#"):
                channels.append({"name": current_name or f"Canal_{len(channels)+1}",
                                 "url": line, "headers": current_headers})
                current_name, current_headers = None, None
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

def _safe_qsize(q: Queue) -> int:
    try:
        return q.qsize()
    except NotImplementedError:
        return 0 if q.empty() else 1

def write_status(channels: list[Channel], heartbeats: dict,
                 inference_workers: list[InferenceWorker]):
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
            "queue_size": _safe_qsize(ch.out_queue) if ch.out_queue else 0,
        })
    inf_state = []
    for iw in inference_workers:
        alive = iw.process is not None and iw.process.is_alive()
        ready = bool(iw.ready_event and iw.ready_event.is_set())
        inf_state.append({
            "name":       iw.name,
            "device":     iw.device,
            "threads":    iw.threads,
            "weight":     iw.weight,
            "alive":      alive,
            "ready":      ready,
            "restarts":   iw.restarts,
            "queue_size": _safe_qsize(iw.in_queue) if iw.in_queue else 0,
            "queue_max":  iw.maxsize,
            "assigned":   iw.assigned,
        })
    payload = {
        "updated_at": now.isoformat(sep=" ", timespec="seconds"),
        "model":      TRANSCRIBER_MODEL,
        "inference_workers": inf_state,
        "channels":         estado,
    }
    try:
        STATUS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

# ── Control de procesos ───────────────────────────────────────────────────────
def start_channel_worker(ch: Channel) -> Process:
    """Cada worker tiene su propia cola maxsize=3 con drop-oldest."""
    if ch.out_queue is None:
        ch.out_queue = Queue(maxsize=CHANNEL_QUEUE_MAXSIZE)
    p = Process(
        target=worker.run_worker,
        args=(ch.id, ch.name, ch.url, ch.out_queue, ch.headers),
        name=f"worker-{ch.id:02d}",
        daemon=True,
    )
    p.start()
    logger.info(f"[Canal {ch.id:02d}] Worker iniciado PID={p.pid} name='{ch.name}'")
    return p

def start_inference_worker(iw: InferenceWorker) -> Process:
    """Cada worker tiene su propia cola de entrada (creada si aún no existe)."""
    iw.ready_event = MPEvent()
    if iw.in_queue is None:
        iw.in_queue = Queue(maxsize=iw.maxsize)
    p = Process(
        target=transcriber_mod.run,
        kwargs={
            "audio_queue":  iw.in_queue,
            "model_name":   TRANSCRIBER_MODEL,
            "device":       iw.device,
            "worker_name":  iw.name,
            "threads":      iw.threads,
            "ready_event":  iw.ready_event,
        },
        name=f"inference-{iw.name}",
        daemon=True,
    )
    p.start()
    logger.info(f"Inference worker '{iw.name}' PID={p.pid} "
                f"device={iw.device} threads={iw.threads} "
                f"weight={iw.weight} maxsize={iw.maxsize}")
    return p

def stop_all(channels: list[Channel], inference_workers: list[InferenceWorker]):
    logger.info("Deteniendo todos los procesos...")
    todos = [ch.process for ch in channels if ch.process]
    todos += [iw.process for iw in inference_workers if iw.process]
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

# ── Dispatcher ────────────────────────────────────────────────────────────────
def _pick_worker(inference_workers: list[InferenceWorker]) -> Optional[InferenceWorker]:
    """
    Round-robin ponderado por weight:
      - Solo considera workers con cola no llena
      - Entre los disponibles, elige aquel con menor (assigned / weight)
        → el más subutilizado respecto a su cuota relativa
    """
    available = [iw for iw in inference_workers
                 if iw.in_queue is not None and _safe_qsize(iw.in_queue) < iw.maxsize]
    if not available:
        return None
    return min(available, key=lambda iw: iw.assigned / max(iw.weight, 1))

def dispatcher_loop(channels: list[Channel],
                    inference_workers: list[InferenceWorker],
                    stop_event: threading.Event):
    """
    Empuja chunks de las colas por canal a la cola del worker que toque según
    el round-robin ponderado. Si TODAS las colas de workers están llenas,
    espera (los channel workers harán drop-oldest por su cuenta si se saturan).
    """
    poll = DISPATCHER_POLL_MS / 1000.0
    while not stop_event.is_set():
        # 1. ¿Qué canal está más atrasado?
        best_ch   = None
        best_size = 0
        for ch in channels:
            if ch.out_queue is None:
                continue
            sz = _safe_qsize(ch.out_queue)
            if sz > best_size:
                best_size = sz
                best_ch   = ch
        if best_ch is None:
            stop_event.wait(poll)
            continue

        # 2. ¿Qué worker debe tomarlo?
        iw = _pick_worker(inference_workers)
        if iw is None:
            # Todas las colas de workers llenas: backpressure natural, esperamos
            stop_event.wait(poll)
            continue

        # 3. Sacar del canal y encolar al worker
        try:
            item = best_ch.out_queue.get_nowait()
        except _queue.Empty:
            continue
        try:
            iw.in_queue.put(item, timeout=1.0)
            iw.assigned += 1
        except _queue.Full:
            logger.warning(f"[dispatcher] cola {iw.name} llena — chunk canal {best_ch.id:02d} perdido")

# ── Health check ──────────────────────────────────────────────────────────────
def health_check(channels: list[Channel],
                 inference_workers: list[InferenceWorker]):
    heartbeats = get_heartbeats()
    now        = datetime.now()

    # Inference workers
    for iw in inference_workers:
        if iw.process and not iw.process.is_alive():
            logger.error(f"Inference worker '{iw.name}' caído — reiniciando...")
            record_failure(Channel(0, f"inference-{iw.name}", ""),
                           f"Inference worker '{iw.name}' caído", "Reinicio")
            iw.restarts += 1
            iw.last_restart = datetime.now()
            iw.process = start_inference_worker(iw)

    # Channel workers
    for ch in channels:
        if ch.failed:
            continue

        alive = ch.process is not None and ch.process.is_alive()

        if not alive:
            exit_code = ch.process.exitcode if ch.process else "N/A"
            reason = f"Worker terminó (código={exit_code})"
            ch.restarts += 1
            ch.last_restart = datetime.now()
            logger.warning(f"[Canal {ch.id:02d}] {reason} — reinicio #{ch.restarts}")
            record_failure(ch, reason, f"Reinicio #{ch.restarts}")
            time.sleep(2)
            ch.process = start_channel_worker(ch)
            continue

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
                ch.process = start_channel_worker(ch)

    write_status(channels, heartbeats, inference_workers)

ANSI_CLEAR    = "\033[2J\033[H"
ANSI_BOLD     = "\033[1m"
ANSI_RESET    = "\033[0m"
ANSI_GREEN    = "\033[32m"
ANSI_YELLOW   = "\033[33m"
ANSI_RED      = "\033[31m"
ANSI_CYAN     = "\033[36m"
ANSI_DIM      = "\033[2m"
ANSI_HIDE_CUR = "\033[?25l"
ANSI_SHOW_CUR = "\033[?25h"

def print_dashboard(channels: list[Channel],
                    inference_workers: list[InferenceWorker],
                    started_at: datetime):
    heartbeats = get_heartbeats()
    now        = datetime.now()
    uptime     = int((now - started_at).total_seconds())
    h, m, s    = uptime // 3600, (uptime % 3600) // 60, uptime % 60

    try:
        cols = os.get_terminal_size().columns
    except Exception:
        cols = 100
    cols = max(cols, 80)
    line = "─" * cols

    # Badge de inference pool
    badges = []
    for iw in inference_workers:
        alive = iw.process is not None and iw.process.is_alive()
        ready = bool(iw.ready_event and iw.ready_event.is_set())
        if alive and ready:
            c, s_ = ANSI_GREEN, "●"
        elif alive:
            c, s_ = ANSI_YELLOW, "○"
        else:
            c, s_ = ANSI_RED, "✖"
        badges.append(f"{c}{s_}{ANSI_RESET}{iw.name}")
    pool_badge = " ".join(badges)

    buf = [ANSI_CLEAR, ANSI_HIDE_CUR]
    buf.append(f"{ANSI_BOLD}{'TRANSCRIBER MANAGER (pool GPU+CPU / Qwen3-ASR)':^{cols}}{ANSI_RESET}\n")
    buf.append(f"{ANSI_DIM}{line}{ANSI_RESET}\n")
    buf.append(
        f"  {ANSI_BOLD}Modelo:{ANSI_RESET} {TRANSCRIBER_MODEL}"
        f"   Pool: {pool_badge}"
        f"   {ANSI_BOLD}Uptime:{ANSI_RESET} {h:02d}:{m:02d}:{s:02d}"
        f"   {ANSI_DIM}{now.strftime('%H:%M:%S')}{ANSI_RESET}\n"
    )
    buf.append(f"{ANSI_DIM}{line}{ANSI_RESET}\n")
    buf.append(
        f"  {'#':>2}  {'Canal':<22}  {'Proceso':<8}  {'Heartbeat':>9}  "
        f"{'Q':>2}  {'Rst':>4}  {'Rst/h':>5}  {'Estabilidad':<14}  {'Último corte'}\n"
    )
    buf.append(f"{ANSI_DIM}{line}{ANSI_RESET}\n")

    uptime_h = max(uptime / 3600, 1 / 60)

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

        # Cola del canal: q=0 (verde), q=1-2 (amarillo), q=3 (rojo saturado)
        qsz = _safe_qsize(ch.out_queue) if ch.out_queue else 0
        if qsz == 0:
            q_col = f"{ANSI_DIM}{qsz:>2}{ANSI_RESET}"
        elif qsz < CHANNEL_QUEUE_MAXSIZE:
            q_col = f"{ANSI_YELLOW}{qsz:>2}{ANSI_RESET}"
        else:
            q_col = f"{ANSI_RED}{qsz:>2}{ANSI_RESET}"

        buf.append(
            f"  {ch.id:>2}  {nombre:<22}  {proc_col}  {hb_col}  "
            f"{q_col}  {rst_col}  {rst_h_str}  {stab_col}  {lr_str}\n"
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

    # El id de canal es su posición en el M3U completo (1-based), NO la posición
    # tras filtrar — así dos instancias de manager.py (split por motor) asignan
    # el MISMO channel_id al mismo canal y no chocan en BD/archivos.
    channels = []
    for i, ch in enumerate(raw[:MAX_CHANNELS], start=1):
        if i in SKIP_CHANNELS:
            continue
        name = ch["name"]
        if ONLY_CHANNELS and name not in ONLY_CHANNELS:
            continue
        if SKIP_CHANNEL_NAMES and name in SKIP_CHANNEL_NAMES:
            continue
        channels.append(Channel(id=i, name=name, url=ch["url"], headers=ch.get("headers")))
    logger.info(f"Canales activos: {len(channels)} de {len(raw[:MAX_CHANNELS])} "
                f"(omitidos por índice: {sorted(SKIP_CHANNELS)}, "
                f"only={sorted(ONLY_CHANNELS) or '—'}, skip_names={sorted(SKIP_CHANNEL_NAMES) or '—'})")

    # Pool de inferencia, cada worker con su propia cola
    inference_workers = [InferenceWorker(name=n, device=d, threads=t,
                                         weight=w, maxsize=mx)
                         for (n, d, t, w, mx) in INFERENCE_POOL]

    dispatcher_stop  = threading.Event()
    dispatcher_thread: Optional[threading.Thread] = None

    def cleanup():
        dispatcher_stop.set()
        stop_all(channels, inference_workers)

    atexit.register(cleanup)

    # Lanzar inference workers primero (tardan en cargar el modelo)
    logger.info(f"Iniciando pool de inferencia ({len(inference_workers)} workers)...")
    for iw in inference_workers:
        iw.process = start_inference_worker(iw)

    # Esperar a que TODOS estén ready (vía mp.Event). Sin timeout fijo.
    logger.info("Esperando que los modelos carguen en cada worker...")
    for iw in inference_workers:
        if not iw.ready_event.wait(timeout=300):
            logger.error(f"Worker '{iw.name}' no arrancó en 5 min — abortando")
            cleanup()
            sys.exit(1)
        logger.info(f"  ✓ '{iw.name}' listo")

    # Lanzar workers de audio
    logger.info("Iniciando workers de audio (1 por canal)...")
    for ch in channels:
        ch.process = start_channel_worker(ch)
        time.sleep(2)   # leve escalonamiento para evitar flood al servidor de streaming

    # Arrancar dispatcher
    dispatcher_thread = threading.Thread(
        target=dispatcher_loop,
        args=(channels, inference_workers, dispatcher_stop),
        name="dispatcher",
        daemon=True,
    )
    dispatcher_thread.start()
    logger.info("Dispatcher en línea — el sistema está operativo")

    started_at = datetime.now()

    def handle_exit(*_):
        sys.stdout.write(ANSI_SHOW_CUR + "\n")
        sys.stdout.flush()
        cleanup()
        os._exit(0)

    signal.signal(signal.SIGINT,  handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    print_dashboard(channels, inference_workers, started_at)

    while True:
        time.sleep(HEALTH_INTERVAL)
        health_check(channels, inference_workers)
        print_dashboard(channels, inference_workers, started_at)


if __name__ == "__main__":
    main()
