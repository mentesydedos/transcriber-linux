"""
transcriber.py — Worker de inferencia Qwen3-ASR-1.7B (GPU o CPU)

OBSOLETO (2026-08-07): este fue el motor ORIGINAL, reemplazado por Parakeet
(transcriber_parakeet.py) y Cohere (transcriber_cohere.py) — ver
TRANSCRIBER_ENGINE en manager.py. Se dejó de usar en producción hace tiempo,
y desde entonces el sistema creció de 8 a 51 canales (26 TV + 25 radio FM)
con una arquitectura de split entre dos motores que este archivo no conoce.
GPU_BATCH_SIZE=4 sigue tuneado para la T1000 8GB que ya no está en uso (la
máquina corre una RTX 4070 12GB desde 2026-08). NO reactivar sin antes:
  1. Decidir a cuáles de los 51 canales aplicaría (hoy asume "todos").
  2. Re-tunear GPU_BATCH_SIZE / INFERENCE_POOL para la 4070.
  3. Revisar que transcriber.service siga siendo compatible con el resto
     del pipeline (M3U con radio, columnas nuevas en las tablas, etc.)
Se conserva el código tal cual como referencia histórica, no como rollback
listo para usarse.

Arquitectura (histórica, como corría con 8 canales en la T1000):
  - Se lanzan N instancias en paralelo (multiprocessing), cada una con su copia
    del modelo. Típico: 1× GPU (batch=4, fp16) + 2× CPU (batch=1, fp32, 8 hilos).
  - Todas leen de una jobs_queue compartida que alimenta el manager vía dispatcher.
  - Cada item de la cola: {channel_id, channel_name, audio, chunk_sec}.
"""

import os
import sqlite3
import time
import logging
import sys
import threading
import queue as _queue
import numpy as np
import torch
from datetime import datetime, timedelta
from pathlib import Path
from qwen_asr import Qwen3ASRModel

# ── Config ────────────────────────────────────────────────────────────────────
MODEL_NAME        = "Qwen/Qwen3-ASR-0.6B"
MODEL_CACHE_DIR   = "./models"
SAMPLE_RATE       = 16000
LANGUAGE          = "Spanish"        # None = detección automática
GPU_BATCH_SIZE    = 4                # hasta 4 chunks simultáneos en la T1000 8GB
CPU_BATCH_SIZE    = 1                # en CPU batch>1 solo añade overhead
BATCH_WAIT_MS     = 150              # esperar 150ms para agrupar chunks antes de inferir (solo GPU)
MAX_NEW_TOKENS    = 128              # 128 basta para ~30s de habla densa; 256 ralentiza 20-30% sin mejorar output

FILE_WINDOW_MIN   = 30
DB_PATH           = os.environ.get("TRANSCRIBER_DB", "transcriptions.db")
ALERTS_DB         = Path("alerts.db")
OUTPUT_DIR        = Path("output")
LOG_DIR           = Path("logs")

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ── Logging ───────────────────────────────────────────────────────────────────
def setup_logger(worker_name: str = "transcriber") -> logging.Logger:
    """Un logger por worker. worker_name ∈ {'gpu', 'cpu-1', 'cpu-2', ...}."""
    LOG_DIR.mkdir(exist_ok=True)
    logger = logging.getLogger(f"transcriber.{worker_name}")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        # Archivo común para todos los workers (fácil de seguir con tail)
        fh = logging.FileHandler(LOG_DIR / "transcriber.log", encoding='utf-8')
        fh.setFormatter(logging.Formatter(
            f"%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
        logger.addHandler(fh)
        sh = logging.StreamHandler()
        sh.setLevel(logging.ERROR)
        sh.setFormatter(logging.Formatter(
            f"%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
        logger.addHandler(sh)
    return logger

# ── Base de datos ─────────────────────────────────────────────────────────────
_db_lock = threading.Lock()

def save_to_db(channel_id: int, channel_name: str, text: str,
               duration: float, start_ts: str = None, source: str = "asr") -> str:
    """Guarda la transcripción en DB.
    start_ts: ISO de CUANDO se transmitió el audio (no cuando se inferió).
              Si es None, usa now() como fallback (modo legado).
    source: 'asr' (Qwen3) o 'cc' (ccextractor). Mismo destino/FTS → RAG, búsqueda
            y alertas lo consumen igual.
    """
    if start_ts:
        # Parseamos para obtener unix_ts consistente con el timestamp textual
        try:
            dt = datetime.fromisoformat(start_ts)
        except ValueError:
            dt = datetime.now()
        ts = start_ts
    else:
        dt = datetime.now()
        ts = dt.isoformat(sep=" ", timespec="milliseconds")
    unix_ts = dt.timestamp()
    with _db_lock:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.execute(
                """INSERT INTO transcriptions
                   (channel_id, channel_name, timestamp, unix_ts, text, confidence, duration_sec, source)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (channel_id, channel_name, ts, unix_ts, text, None, duration, source))
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

def _srt_time(dt: datetime, origin: datetime) -> str:
    """HH:MM:SS,mmm desde origin (inicio del bloque SRT)."""
    delta = dt - origin
    if delta.total_seconds() < 0:
        delta = timedelta(0)
    total_ms = int(delta.total_seconds() * 1000)
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms  = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

class FileWindow:
    """Escribe TXT legible y SRT (compatible con reproductores de video) por
    bloques de 30 min. El SRT tiene timestamps relativos al inicio del bloque,
    el TXT tiene timestamps absolutos con milisegundos."""
    def __init__(self, channel_id: int, channel_name: str):
        self.channel_id   = channel_id
        self.channel_name = channel_name
        self.safe_name    = "".join(c if c.isalnum() else "_" for c in channel_name)
        self.base         = f"canal_{channel_id:02d}_{self.safe_name}"
        self.window_start = None   # inicio del bloque (origin para SRT)
        self.window_end   = None
        self.fh_txt       = None
        self.fh_srt       = None
        self.srt_idx      = 0
        self._rotate(datetime.now())

    def _rotate(self, now: datetime):
        for fh in (self.fh_txt, self.fh_srt):
            if fh and not fh.closed:
                fh.flush(); fh.close()
        start, end = _window_bounds(now)
        self.window_start = start
        self.window_end   = end
        self.srt_idx      = 0
        folder = OUTPUT_DIR / (start.strftime("%Y-%m-%d_%H-%M") + end.strftime("_%H-%M"))
        folder.mkdir(parents=True, exist_ok=True)
        txt_path = folder / f"{self.base}.txt"
        # Solo escribir el header si el archivo es nuevo: evita duplicarlo cuando
        # el proceso se reinicia a mitad de un bloque (otro FileWindow ya lo escribió).
        is_new = not txt_path.exists() or txt_path.stat().st_size == 0
        self.fh_txt = open(txt_path, "a", encoding="utf-8")
        self.fh_srt = open(folder / f"{self.base}.srt", "a", encoding="utf-8")
        if is_new:
            ts_str    = now.strftime('%Y-%m-%d %H:%M:%S')
            programme = _get_epg_programme(self.channel_name, ts_str)
            header    = f"[EPG] {programme}" if programme else "[EPG] sin datos"
            self.fh_txt.write(f"# {header}\n"
                              f"# Bloque: {start.isoformat(sep=' ')} → {end.isoformat(sep=' ')}\n"
                              f"# Formato: [start → end] texto\n")
            self.fh_txt.flush()

    def write(self, start_ts: str, end_ts: str, text: str):
        """start_ts/end_ts: ISO con milisegundos (tiempo real del audio)."""
        try:
            start_dt = datetime.fromisoformat(start_ts)
            end_dt   = datetime.fromisoformat(end_ts)
        except Exception:
            start_dt = end_dt = datetime.now()

        if start_dt >= self.window_end:
            self._rotate(start_dt)

        # TXT legible con timestamp absoluto [start → end]
        self.fh_txt.write(f"[{start_ts} → {end_ts[11:]}] {text}\n")
        self.fh_txt.flush()

        # SRT con timestamps relativos al inicio del bloque
        self.srt_idx += 1
        self.fh_srt.write(
            f"{self.srt_idx}\n"
            f"{_srt_time(start_dt, self.window_start)} --> "
            f"{_srt_time(end_dt, self.window_start)}\n"
            f"{text}\n\n"
        )
        self.fh_srt.flush()

    def close(self):
        for fh in (self.fh_txt, self.fh_srt):
            if fh and not fh.closed:
                fh.flush(); fh.close()

# ── Inferencia Qwen3-ASR ──────────────────────────────────────────────────────
def _is_silent_or_noise(text: str) -> bool:
    """Detecta si el texto devuelto es silencio, música, o un marcador vacío."""
    if not text:
        return True
    cleaned = text.strip().strip(".,;:¿?¡!—-–…").lower()
    if not cleaned:
        return True
    # Marcadores comunes devueltos por Qwen3-ASR cuando no hay habla clara
    markers = {"[música]", "[music]", "(música)", "(music)", "[silencio]", "[silence]"}
    return cleaned in markers

def transcribe_batch(model: Qwen3ASRModel, audios: list[np.ndarray]) -> list[str]:
    """Inferencia batch. Devuelve lista de strings (vacío si es silencio/ruido)."""
    batch = [(a, SAMPLE_RATE) for a in audios]
    results = model.transcribe(
        audio=batch,
        language=LANGUAGE,
    )
    out = []
    for r in results:
        text = (r.text or "").strip()
        out.append("" if _is_silent_or_noise(text) else text)
    return out

# ── Loop principal ────────────────────────────────────────────────────────────
def run(audio_queue, model_name: str = MODEL_NAME, device: str = "cuda",
        worker_name: str = None, threads: int = None,
        batch_size: int = None, ready_event=None):
    """
    Worker de inferencia. Una instancia por proceso.

    Args:
      audio_queue : mp.Queue con items {channel_id, channel_name, audio, chunk_sec}
      device      : 'cuda' | 'cpu'
      worker_name : etiqueta para los logs ('gpu', 'cpu-1', ...). Autogenerada si None.
      threads     : hilos CPU cuando device='cpu' (ignorado en GPU)
      batch_size  : override; por defecto 4 en GPU, 1 en CPU
      ready_event : mp.Event que se setea cuando el modelo terminó de cargar + warmup
    """
    # Defaults por device
    if worker_name is None:
        worker_name = device
    if batch_size is None:
        batch_size = GPU_BATCH_SIZE if device == "cuda" else CPU_BATCH_SIZE

    # Hilos CPU (debe hacerse ANTES de importar/usar torch ops)
    if device == "cpu" and threads is not None:
        torch.set_num_threads(threads)
        os.environ.setdefault("OMP_NUM_THREADS", str(threads))
        os.environ.setdefault("MKL_NUM_THREADS", str(threads))

    logger = setup_logger(worker_name)

    if device == "cuda":
        if not torch.cuda.is_available():
            logger.error("CUDA no disponible — worker GPU no puede arrancar")
            sys.exit(1)
        gpu_name = torch.cuda.get_device_name(0)
        gpu_cap  = torch.cuda.get_device_capability(0)
        use_bf16 = gpu_cap[0] >= 8
        dtype    = torch.bfloat16 if use_bf16 else torch.float16
        dtype_str = "bf16" if use_bf16 else "fp16"
        device_map = "cuda:0"
        logger.info(f"GPU: {gpu_name} (compute {gpu_cap[0]}.{gpu_cap[1]}) — dtype={dtype_str}")
    elif device == "cpu":
        dtype     = torch.float32
        dtype_str = "fp32"
        device_map = "cpu"
        logger.info(f"CPU worker — hilos={threads or 'default'} dtype={dtype_str}")
    else:
        logger.error(f"device desconocido: {device!r}")
        sys.exit(1)

    logger.info(f"Cargando {model_name} (batch_size={batch_size})...")

    Path(MODEL_CACHE_DIR).mkdir(exist_ok=True)

    model = Qwen3ASRModel.from_pretrained(
        model_name,
        dtype=dtype,
        device_map=device_map,
        max_inference_batch_size=batch_size,
        max_new_tokens=MAX_NEW_TOKENS,
        cache_dir=MODEL_CACHE_DIR,
    )

    logger.info("Pre-calentando el modelo...")
    try:
        warmup_audio = np.zeros(SAMPLE_RATE, dtype=np.float32)
        transcribe_batch(model, [warmup_audio])
    except Exception as e:
        logger.warning(f"Warmup falló (no crítico): {e}")

    logger.info("Modelo listo — iniciando loop de inferencia")
    if ready_event is not None:
        ready_event.set()

    file_windows: dict[int, FileWindow] = {}
    stats: dict[int, int] = {}

    def _drain_queue(first_item: dict, max_items: int) -> list[dict]:
        """Recoge hasta max_items de la cola (el primero ya lo tenemos).
        Solo útil cuando batch_size > 1 (GPU)."""
        batch = [first_item]
        if max_items <= 1:
            return batch
        deadline = time.time() + (BATCH_WAIT_MS / 1000.0)
        while len(batch) < max_items:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                batch.append(audio_queue.get(timeout=remaining))
            except Exception:
                break
        return batch

    last_stats = time.time()

    while True:
        try:
            first = audio_queue.get(timeout=5)
        except Exception:
            if time.time() - last_stats > 300:
                logger.info(f"Idle | Chunks procesados: {stats}")
                last_stats = time.time()
            continue

        batch = _drain_queue(first, batch_size)

        t0 = time.time()
        try:
            texts = transcribe_batch(model, [item["audio"] for item in batch])
        except Exception as e:
            logger.error(f"Error en inferencia batch: {e}", exc_info=True)
            continue
        elapsed = time.time() - t0

        for item, text in zip(batch, texts):
            cid       = item["channel_id"]
            cname     = item["channel_name"]
            chunk_sec = item.get("chunk_sec", 30)
            start_ts  = item.get("start_ts")    # tiempo real del audio (no de inferencia)
            end_ts    = item.get("end_ts")

            if cid not in file_windows:
                file_windows[cid] = FileWindow(cid, cname)

            try:
                save_to_db(cid, cname, text or "[~]", chunk_sec, start_ts=start_ts)
            except Exception as e:
                logger.error(f"[{cid:02d}] Error guardando en DB: {e}")
                continue

            if text and start_ts and end_ts:
                try:
                    file_windows[cid].write(start_ts, end_ts, text)
                except Exception as e:
                    logger.error(f"[{cid:02d}] Error escribiendo archivo: {e}")

                logger.info(f"[{cid:02d}]({elapsed:.1f}s/bs={len(batch)}) {text[:80]}")
            elif text:
                logger.warning(f"[{cid:02d}] item sin start_ts/end_ts (modo legado)")
            else:
                logger.debug(f"[{cid:02d}] sin voz ({elapsed:.1f}s)")

            stats[cid] = stats.get(cid, 0) + 1

        if time.time() - last_stats > 300:
            logger.info(f"Chunks: {stats}")
            last_stats = time.time()


if __name__ == "__main__":
    from multiprocessing import Queue
    q = Queue()
    run(q, device="cuda")
