"""
transcriber.py — Servidor centralizado de inferencia Whisper
Arquitectura: N hilos persistentes, cada uno bloquea en la cola y procesa
en cuanto llega un chunk. Sin batching → sin gaps de pipeline.
"""

import sqlite3
import time
import logging
import sys
import threading
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from faster_whisper import WhisperModel

# ── Config ────────────────────────────────────────────────────────────────────
MODEL_NAME   = "small"
DEVICE       = "cpu"
# int8 CPU: misma calidad que float16, más rápido en CPU
# 8 workers × 4 threads = 32 hilos causan contención severa de memoria/BLAS
# 4 workers × 8 threads = 32 hilos, cada worker tiene recursos dedicados → 2-3s/chunk
PARALLEL_WORKERS = 4
CPU_THREADS      = 8

FILE_WINDOW_MIN = 30
DB_PATH         = "transcriptions.db"
ALERTS_DB       = Path("alerts.db")
OUTPUT_DIR      = Path("output")
LOG_DIR         = Path("logs")

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ── Logging ───────────────────────────────────────────────────────────────────
def setup_logger() -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    logger = logging.getLogger("transcriber")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        # Solo archivo — el terminal del manager muestra su propio dashboard limpio
        fh = logging.FileHandler(LOG_DIR / "transcriber.log", encoding='utf-8')
        fh.setFormatter(logging.Formatter("%(asctime)s [TRANSCRIBER] %(levelname)s: %(message)s"))
        logger.addHandler(fh)
        # Errores críticos sí aparecen en consola
        sh = logging.StreamHandler()
        sh.setLevel(logging.ERROR)
        sh.setFormatter(logging.Formatter("%(asctime)s [TRANSCRIBER] %(levelname)s: %(message)s"))
        logger.addHandler(sh)
    return logger

# ── Base de datos ─────────────────────────────────────────────────────────────
_db_lock = threading.Lock()   # serializa escrituras entre hilos

def save_to_db(channel_id: int, channel_name: str, text: str,
               confidence: float, duration: float) -> str:
    now     = datetime.now()
    ts      = now.isoformat(sep=" ", timespec="seconds")
    unix_ts = now.timestamp()
    with _db_lock:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.execute(
                """INSERT INTO transcriptions
                   (channel_id, channel_name, timestamp, unix_ts, text, confidence, duration_sec)
                   VALUES (?,?,?,?,?,?,?)""",
                (channel_id, channel_name, ts, unix_ts, text, confidence, duration))
            rowid = cursor.lastrowid
            conn.execute(
                "INSERT INTO transcriptions_fts(rowid, text, channel_name) VALUES (?,?,?)",
                (rowid, text, channel_name))
            conn.execute(
                "UPDATE channel_status SET last_seen=?, total_segments=total_segments+1 WHERE channel_id=?",
                (ts, channel_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return ts

# ── EPG ───────────────────────────────────────────────────────────────────────
def _get_epg_programme(channel_name: str, timestamp: str) -> str:
    """Retorna el título del programa EPG activo en el momento dado, o ''."""
    try:
        conn = sqlite3.connect(str(ALERTS_DB), timeout=5)
        row  = conn.execute("""
            SELECT title FROM epg_programmes
            WHERE channel_name = ? AND start_ts <= ? AND stop_ts > ?
            ORDER BY start_ts DESC LIMIT 1
        """, (channel_name, timestamp, timestamp)).fetchone()
        conn.close()
        return (row[0] or '').strip() if row else ''
    except Exception:
        return ''


# ── Rotación de archivos ──────────────────────────────────────────────────────
def _window_bounds(dt: datetime):
    minute = (dt.minute // FILE_WINDOW_MIN) * FILE_WINDOW_MIN
    start  = dt.replace(minute=minute, second=0, microsecond=0)
    end    = start + timedelta(minutes=FILE_WINDOW_MIN)
    return start, end

class FileWindow:
    def __init__(self, channel_id: int, channel_name: str):
        self.channel_id   = channel_id
        self.channel_name = channel_name
        self.safe_name    = "".join(c if c.isalnum() else "_" for c in channel_name)
        self.filename     = f"canal_{channel_id:02d}_{self.safe_name}.txt"
        self.window_end   = None
        self.fh           = None
        self._rotate(datetime.now())

    def _rotate(self, now: datetime):
        if self.fh and not self.fh.closed:
            self.fh.flush(); self.fh.close()
        start, end = _window_bounds(now)
        self.window_end = end
        folder = OUTPUT_DIR / (start.strftime("%Y-%m-%d_%H-%M") + end.strftime("_%H-%M"))
        folder.mkdir(parents=True, exist_ok=True)
        self.fh = open(folder / self.filename, "a", encoding="utf-8")
        # Cabecera EPG al inicio de cada ventana de media hora
        ts_str    = now.strftime('%Y-%m-%d %H:%M:%S')
        programme = _get_epg_programme(self.channel_name, ts_str)
        header    = f"[EPG] {programme}" if programme else "[EPG] sin datos"
        self.fh.write(f"{header}\n")
        self.fh.flush()

    def write(self, ts: str, text: str):
        now = datetime.now()
        if now >= self.window_end:
            self._rotate(now)
        self.fh.write(f"[{ts}] {text}\n")
        self.fh.flush()

    def close(self):
        if self.fh and not self.fh.closed:
            self.fh.flush(); self.fh.close()

# ── Transcripción ─────────────────────────────────────────────────────────────
def transcribe_chunk(model: WhisperModel, audio: np.ndarray) -> tuple[str, float]:
    segments, _ = model.transcribe(
        audio,
        language="es",
        beam_size=2,
        vad_filter=True,
        vad_parameters=dict(
            min_silence_duration_ms=500,
            speech_pad_ms=100,
            threshold=0.5,        # más estricto: filtra mejor música de fondo
        ),
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
        compression_ratio_threshold=2.4,
    )
    texts, confidences = [], []
    for seg in segments:
        if seg.no_speech_prob >= 0.6 or seg.avg_logprob < -1.0:
            continue
        t = seg.text.strip()
        if not t:
            continue
        texts.append(t)
        confidence = min(1.0, max(0.0, (seg.avg_logprob + 1.2) / 1.0))
        confidences.append(confidence)
    text       = " ".join(texts).strip()
    confidence = float(np.mean(confidences)) if confidences else 0.0
    return text, confidence

# ── Loop principal ────────────────────────────────────────────────────────────
def run(audio_queue, model_name: str = MODEL_NAME, device: str = DEVICE,
        parallel_workers: int = PARALLEL_WORKERS, cpu_threads: int = CPU_THREADS):
    logger = setup_logger()
    compute = "float16" if device == "cuda" else "int8"
    logger.info(f"Cargando '{model_name}' en {device} ({compute}) — {parallel_workers} workers × {cpu_threads} threads...")

    model = WhisperModel(
        model_name,
        device=device,
        compute_type=compute,
        cpu_threads=cpu_threads,        # intra_threads: cores por inferencia
        download_root="./models",
        num_workers=parallel_workers,   # inter_threads: N inferencias simultáneas en CTranslate2
    )

    # Pre-calentar modelo y VAD antes de abrir hilos
    logger.info("Pre-calentando VAD y JIT...")
    try:
        transcribe_chunk(model, np.zeros(16000, dtype=np.float32))
    except Exception:
        pass
    logger.info(f"Modelo listo. {PARALLEL_WORKERS} hilos persistentes arrancando...")

    file_windows: dict[int, FileWindow] = {}
    file_lock  = threading.Lock()
    stats: dict[int, int] = {}
    stats_lock = threading.Lock()

    def process_one(item: dict):
        """Transcribe un chunk y guarda. Llamado desde hilo worker."""
        cid       = item["channel_id"]
        cname     = item["channel_name"]
        audio     = item["audio"]
        chunk_sec = item.get("chunk_sec", 8)

        # Asegurar FileWindow (con lock)
        with file_lock:
            if cid not in file_windows:
                file_windows[cid] = FileWindow(cid, cname)

        # Inferencia GPU/CPU (paralela — CTranslate2 inter_threads lo permite)
        t0 = time.time()
        text, confidence = transcribe_chunk(model, audio)
        elapsed = time.time() - t0

        # Guardar en DB (serializado por _db_lock interno)
        ts = save_to_db(cid, cname, text or "[~]", confidence, chunk_sec)

        # Escribir a archivo (con lock)
        with file_lock:
            if text:
                file_windows[cid].write(ts, text)

        return cid, text, confidence, elapsed

    def worker_loop(worker_id: int):
        """
        Hilo persistente: bloquea en la cola y procesa en cuanto llega un chunk.
        No espera lotes — reacciona al instante. Clave para fluidez real.
        """
        while True:
            try:
                item = audio_queue.get(timeout=5)
            except Exception:
                continue   # timeout normal — seguir esperando
            try:
                cid, text, conf, elapsed = process_one(item)
                if text:
                    logger.info(f"[{cid:02d}][{conf:.2f}]({elapsed:.1f}s) {text[:80]}")
                else:
                    logger.debug(f"[{cid:02d}] sin voz ({elapsed:.1f}s)")
                with stats_lock:
                    stats[cid] = stats.get(cid, 0) + 1
            except Exception as e:
                logger.error(f"[worker-{worker_id}] {e}", exc_info=True)

    # Lanzar N hilos persistentes
    workers = [
        threading.Thread(target=worker_loop, args=(i,), daemon=True, name=f"tw-{i}")
        for i in range(parallel_workers)
    ]
    for w in workers:
        w.start()
    logger.info(f"{parallel_workers} workers activos — sistema listo")

    # Hilo principal: solo monitorea estadísticas
    last_stats = time.time()
    while True:
        time.sleep(30)
        if time.time() - last_stats > 300:
            q_size = audio_queue.qsize()
            with stats_lock:
                s = dict(stats)
            logger.info(f"Cola: {q_size} | Chunks procesados: {s}")
            if q_size > 20:
                logger.warning(f"Cola creciendo ({q_size}) — sistema atrasado")
            last_stats = time.time()


if __name__ == "__main__":
    from multiprocessing import Queue
    q = Queue()
    run(q)
