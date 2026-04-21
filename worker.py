"""
worker.py — Captura de audio de un canal M3U
Solo captura audio con FFmpeg y lo envía a la cola compartida del transcriber.
No carga modelos ni hace inferencia — eso lo hace transcriber.py centralizado.
"""

import subprocess
import numpy as np
import sqlite3
import time
import sys
import logging
import threading
import queue
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
SAMPLE_RATE     = 16000
CHUNK_SECONDS   = 30             # 30s: contexto nativo de Whisper → máxima calidad
CHUNK_SAMPLES   = SAMPLE_RATE * CHUNK_SECONDS
OVERLAP_SEC     = 0              # sin overlap: chunks de 30s son suficientemente largos
OVERLAP_SAMPLES = int(SAMPLE_RATE * OVERLAP_SEC)

DB_PATH = "transcriptions.db"
LOG_DIR = Path("logs")

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ── Logging ───────────────────────────────────────────────────────────────────
def setup_logger(channel_id: int) -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    logger = logging.getLogger(f"canal_{channel_id:02d}")
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_DIR / f"canal_{channel_id:02d}.log", encoding='utf-8')
    fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger

# ── Base de datos ─────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS transcriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER NOT NULL, channel_name TEXT,
        timestamp TEXT NOT NULL, unix_ts REAL NOT NULL,
        text TEXT NOT NULL, confidence REAL, duration_sec REAL)""")
    conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS transcriptions_fts
        USING fts5(text, channel_name, content=transcriptions, content_rowid=id)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS channel_status (
        channel_id INTEGER PRIMARY KEY, channel_name TEXT, url TEXT,
        status TEXT DEFAULT 'stopped', last_seen TEXT, heartbeat TEXT,
        total_segments INTEGER DEFAULT 0, error_count INTEGER DEFAULT 0,
        restart_count INTEGER DEFAULT 0, last_error TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS failure_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER,
        channel_name TEXT, timestamp TEXT, reason TEXT, action TEXT)""")
    # Migración columnas nuevas
    existing = {r[1] for r in conn.execute("PRAGMA table_info(channel_status)").fetchall()}
    for col, defn in [("heartbeat","TEXT"),("restart_count","INTEGER DEFAULT 0"),("last_error","TEXT")]:
        if col not in existing:
            conn.execute(f"ALTER TABLE channel_status ADD COLUMN {col} {defn}")
    conn.commit()
    conn.close()

def update_status(channel_id: int, channel_name: str, url: str, status: str):
    ts = datetime.now().isoformat(sep=" ", timespec="seconds")
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""INSERT OR REPLACE INTO channel_status
                    (channel_id, channel_name, url, status, heartbeat, last_seen)
                    VALUES (?,?,?,?,?,?)""",
                 (channel_id, channel_name, url, status, ts, ts))
    conn.commit()
    conn.close()

def send_heartbeat(channel_id: int, stop_event: threading.Event, interval: int = 30):
    while not stop_event.is_set():
        try:
            ts = datetime.now().isoformat(sep=" ", timespec="seconds")
            conn = sqlite3.connect(DB_PATH, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            # Actualiza heartbeat Y last_seen para que el monitor siempre vea el canal
            conn.execute(
                "UPDATE channel_status SET heartbeat=?, last_seen=? WHERE channel_id=?",
                (ts, ts, channel_id))
            conn.commit()
            conn.close()
        except Exception:
            pass
        stop_event.wait(interval)

# ── FFmpeg ─────────────────────────────────────────────────────────────────────
def detect_channels(url: str, logger: logging.Logger) -> int:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=channels",
             "-of", "default=noprint_wrappers=1:nokey=1", url],
            capture_output=True, text=True, timeout=15)
        n = int(result.stdout.strip())
        logger.info(f"Stream: {n} canales de audio")
        return n
    except Exception as e:
        logger.warning(f"No se pudo detectar canales ({e}), asumiendo estéreo")
        return 2

def start_ffmpeg(url: str, logger: logging.Logger, channels: int = 2) -> subprocess.Popen:
    af_filter = ["-af", "pan=mono|c0=FC"] if channels > 2 else ["-ac", "1"]
    cmd = [
        "ffmpeg",
        "-reconnect", "1", "-reconnect_streamed", "1",
        "-reconnect_delay_max", "3", "-reconnect_at_eof", "1",
        "-timeout", "8000000",
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-f", "ac3", "-i", url,
        "-acodec", "pcm_s16le", "-ar", str(SAMPLE_RATE),
    ] + af_filter + ["-f", "s16le", "pipe:1", "-loglevel", "warning"]
    logger.info(f"FFmpeg iniciado: {url[:60]}... (ch={channels})")
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=CHUNK_SAMPLES * 2)

def ffmpeg_reader(proc: subprocess.Popen, audio_q: queue.Queue,
                  stop_event: threading.Event, logger: logging.Logger):
    bytes_per_chunk = CHUNK_SAMPLES * 2
    remainder = b""
    while not stop_event.is_set():
        try:
            raw = proc.stdout.read(bytes_per_chunk)
            if not raw:
                logger.warning("FFmpeg cerró stdout")
                break
            raw = remainder + raw
            n = len(raw) // (CHUNK_SAMPLES * 2)
            for i in range(n):
                chunk_bytes = raw[i * CHUNK_SAMPLES * 2:(i+1) * CHUNK_SAMPLES * 2]
                audio = np.frombuffer(chunk_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                audio_q.put(audio)
            remainder = raw[n * CHUNK_SAMPLES * 2:]
        except Exception as e:
            logger.error(f"Error leyendo FFmpeg: {e}")
            break

# ── Loop principal ─────────────────────────────────────────────────────────────
def run_worker(channel_id: int, channel_name: str, url: str, shared_queue):
    logger = setup_logger(channel_id)
    logger.info(f"Worker iniciado | canal={channel_name}")
    init_db()
    update_status(channel_id, channel_name, url, "running")

    hb_stop   = threading.Event()
    hb_thread = threading.Thread(target=send_heartbeat, args=(channel_id, hb_stop), daemon=True)
    hb_thread.start()

    audio_channels = detect_channels(url, logger)
    previous_audio = np.zeros(OVERLAP_SAMPLES, dtype=np.float32)
    reconnect_delay = 2
    max_reconnects  = 999

    for attempt in range(max_reconnects):
        if attempt > 0:
            logger.info(f"Reconectando en {reconnect_delay}s (intento {attempt})...")
            time.sleep(reconnect_delay)

        proc       = start_ffmpeg(url, logger, channels=audio_channels)
        local_q    = queue.Queue(maxsize=0)
        stop_event = threading.Event()
        reader     = threading.Thread(target=ffmpeg_reader,
                                      args=(proc, local_q, stop_event, logger), daemon=True)
        reader.start()

        # Descartar primer chunk post-reconexión (audio de transición incompleto)
        if attempt > 0:
            flushed = 0
            while flushed < 1:
                try:
                    local_q.get(timeout=10); flushed += 1
                except queue.Empty:
                    break
            previous_audio = np.zeros(OVERLAP_SAMPLES, dtype=np.float32)
            logger.info(f"Flush post-reconexión: {flushed} chunks descartados")

        try:
            while True:
                try:
                    chunk = local_q.get(timeout=60)
                except queue.Empty:
                    logger.warning("Timeout esperando audio — stream posiblemente caído")
                    break

                audio_with_overlap = np.concatenate([previous_audio, chunk]) if OVERLAP_SAMPLES > 0 else chunk
                previous_audio     = chunk[-OVERLAP_SAMPLES:] if OVERLAP_SAMPLES > 0 else np.zeros(0, dtype=np.float32)

                # Enviar al transcriber centralizado (bloqueante — sin pérdida de datos)
                shared_queue.put({
                    "channel_id":   channel_id,
                    "channel_name": channel_name,
                    "audio":        audio_with_overlap,
                    "chunk_sec":    CHUNK_SECONDS,
                })

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Error en loop: {e}", exc_info=True)
        finally:
            stop_event.set()
            proc.terminate()
            reader.join(timeout=3)

    hb_stop.set()
    logger.info("Worker terminado")


if __name__ == "__main__":
    # Modo legado: ejecución directa sin cola compartida (para pruebas)
    import sys
    if len(sys.argv) != 5:
        print("Uso: python worker.py <id> <nombre> <url> <device>")
        sys.exit(1)
    print("AVISO: Ejecutar via manager.py para usar el transcriber centralizado")
    sys.exit(1)
