"""
transcriber_cohere.py — Worker de inferencia Cohere Transcribe 03-2026 (GPU).

MISMA interfaz run() que transcriber_parakeet.py / transcriber.py: MISMA escritura
a BD/archivos (dashboard, búsqueda, RAG y alertas lo consumen igual). Corre bajo
venv-cohere (transformers 5.14, incompatible con el NeMo de venv-parakeet — por
eso es un proceso/servicio aparte, no un motor más dentro del mismo intérprete).

Por qué existe: Parakeet deriva sistemáticamente al inglés en canales de noticias
(24-43% EN medido en ADN 40/N+/Canal 4, ver transcriber-integracion-parakeet). Cohere
SÍ permite forzar idioma (`language="es"`) → 0% code-switching medido en el A/B
offline. Pensado para servir SOLO esos canales (ver manager.py TRANSCRIBER_ENGINE=
cohere + filtro de canales), con Parakeet cubriendo el resto.

Cohere es un AED (encoder-decoder generativo): a diferencia del RNNT de Parakeet,
NO calla solo ante silencio/ruido — el model card admite que "es ávido de
transcribir" y alucina sobre no-voz. Por eso aquí SÍ hace falta VAD delante
(silero-vad) para no llamar a generate() sobre silencio.
"""
import os
import sqlite3
import time
import logging
import sys
import threading
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
MODEL_NAME        = os.environ.get("TRANSCRIBER_COHERE_MODEL", "CohereLabs/cohere-transcribe-03-2026")
LANGUAGE          = os.environ.get("TRANSCRIBER_COHERE_LANG", "es")
MODEL_CACHE_DIR   = "./models"
SAMPLE_RATE       = 16000
MAX_NEW_TOKENS    = int(os.environ.get("TRANSCRIBER_COHERE_MAX_TOKENS", "256"))
VAD_THRESHOLD     = float(os.environ.get("TRANSCRIBER_COHERE_VAD_THRESHOLD", "0.5"))

FILE_WINDOW_MIN   = 30
DB_PATH           = os.environ.get("TRANSCRIBER_DB", "transcriptions.db")
ALERTS_DB         = Path("alerts.db")
OUTPUT_DIR        = Path("output")
LOG_DIR           = Path("logs")

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ── Logging ───────────────────────────────────────────────────────────────────
def setup_logger(worker_name: str = "transcriber") -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    logger = logging.getLogger(f"transcriber.{worker_name}")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(LOG_DIR / "transcriber.log", encoding='utf-8')
        fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
        logger.addHandler(fh)
        sh = logging.StreamHandler()
        sh.setLevel(logging.ERROR)
        sh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
        logger.addHandler(sh)
    return logger

# ── Base de datos (idéntica a transcriber_parakeet.py) ─────────────────────────
_db_lock = threading.Lock()

def save_to_db(channel_id: int, channel_name: str, text: str,
               duration: float, start_ts: str = None, source: str = "asr") -> str:
    if start_ts:
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

# ── EPG / rotación de archivos (idéntico a transcriber_parakeet.py) ────────────
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

def _window_bounds(dt: datetime):
    minute = (dt.minute // FILE_WINDOW_MIN) * FILE_WINDOW_MIN
    start  = dt.replace(minute=minute, second=0, microsecond=0)
    end    = start + timedelta(minutes=FILE_WINDOW_MIN)
    return start, end

def _srt_time(dt: datetime, origin: datetime) -> str:
    delta = dt - origin
    if delta.total_seconds() < 0:
        delta = timedelta(0)
    total_ms = int(delta.total_seconds() * 1000)
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms  = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

class FileWindow:
    def __init__(self, channel_id: int, channel_name: str):
        self.channel_id   = channel_id
        self.channel_name = channel_name
        self.safe_name    = "".join(c if c.isalnum() else "_" for c in channel_name)
        self.base         = f"canal_{channel_id:02d}_{self.safe_name}"
        self.window_start = None
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
        try:
            start_dt = datetime.fromisoformat(start_ts)
            end_dt   = datetime.fromisoformat(end_ts)
        except Exception:
            start_dt = end_dt = datetime.now()
        if start_dt >= self.window_end:
            self._rotate(start_dt)
        self.fh_txt.write(f"[{start_ts} → {end_ts[11:]}] {text}\n")
        self.fh_txt.flush()
        self.srt_idx += 1
        self.fh_srt.write(
            f"{self.srt_idx}\n"
            f"{_srt_time(start_dt, self.window_start)} --> "
            f"{_srt_time(end_dt, self.window_start)}\n"
            f"{text}\n\n")
        self.fh_srt.flush()

    def close(self):
        for fh in (self.fh_txt, self.fh_srt):
            if fh and not fh.closed:
                fh.flush(); fh.close()

# ── VAD (gate obligatorio: Cohere alucina texto sobre silencio/ruido) ──────────
def _has_speech(vad_model, audio: np.ndarray) -> bool:
    import torch
    from silero_vad import get_speech_timestamps
    ts = get_speech_timestamps(
        torch.from_numpy(audio), vad_model, sampling_rate=SAMPLE_RATE,
        threshold=VAD_THRESHOLD, min_speech_duration_ms=250, return_seconds=True)
    return len(ts) > 0

# ── Loop principal (misma firma que transcriber_parakeet.run) ─────────────────
def run(audio_queue, model_name: str = None, device: str = "cuda",
        worker_name: str = None, threads: int = None,
        batch_size: int = None, ready_event=None):
    import warnings; warnings.filterwarnings("ignore")
    import torch
    from transformers import AutoProcessor, CohereAsrForConditionalGeneration
    from silero_vad import load_silero_vad

    if worker_name is None:
        worker_name = device
    model_id = model_name if (model_name and "cohere" in str(model_name).lower()) else MODEL_NAME

    logger = setup_logger(worker_name)

    if device == "cuda" and not torch.cuda.is_available():
        logger.error("CUDA no disponible — worker Cohere no puede arrancar")
        sys.exit(1)
    if device == "cuda":
        torch.backends.cuda.matmul.allow_tf32 = True
    dev_name = torch.cuda.get_device_name(0) if device == "cuda" else "CPU"
    logger.info(f"Cohere worker '{worker_name}' — device={device} ({dev_name}) lang={LANGUAGE}")

    os.environ.setdefault("HF_HOME", os.path.abspath(MODEL_CACHE_DIR))
    logger.info(f"Cargando {model_id}...")
    # Procesador NATIVO (trust_remote_code=False): el remoto entrega features
    # transpuestas incompatibles con el modelo nativo (ver cohere_transcribe.py).
    processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=False)
    model = CohereAsrForConditionalGeneration.from_pretrained(
        model_id, trust_remote_code=True, device_map="auto" if device == "cuda" else None)
    if device != "cuda":
        model = model.to("cpu")

    logger.info("Cargando VAD (silero)...")
    vad_model = load_silero_vad()

    logger.info("Pre-calentando el modelo...")
    try:
        warm = np.zeros(SAMPLE_RATE, dtype=np.float32)
        inputs = processor(warm, sampling_rate=SAMPLE_RATE, return_tensors="pt", language=LANGUAGE)
        inputs = inputs.to(model.device, dtype=model.dtype)
        model.generate(**inputs, max_new_tokens=8)
    except Exception as e:
        logger.warning(f"Warmup falló (no crítico): {e}")

    logger.info(f"Modelo listo — loop de inferencia (idioma forzado={LANGUAGE}, VAD gate={VAD_THRESHOLD})")
    if ready_event is not None:
        ready_event.set()

    file_windows = {}
    stats = {}
    vad_skipped = {}
    last_stats = time.time()

    while True:
        try:
            item = audio_queue.get(timeout=5)
        except Exception:
            if time.time() - last_stats > 300:
                logger.info(f"Idle | Chunks: {stats} | VAD-skip: {vad_skipped}")
                last_stats = time.time()
            continue

        cid       = item["channel_id"]
        cname     = item["channel_name"]
        chunk     = item["audio"]
        chunk_sec = item.get("chunk_sec", 30)
        start_ts  = item.get("start_ts")
        end_ts    = item.get("end_ts")

        t0 = time.time()
        try:
            if not _has_speech(vad_model, chunk):
                text = ""
                vad_skipped[cid] = vad_skipped.get(cid, 0) + 1
            else:
                inputs = processor(chunk, sampling_rate=SAMPLE_RATE, return_tensors="pt",
                                    language=LANGUAGE)
                inputs = inputs.to(model.device, dtype=model.dtype)
                out = model.generate(**inputs, max_new_tokens=MAX_NEW_TOKENS)
                text = processor.batch_decode(out, skip_special_tokens=True)[0].strip()
        except Exception as e:
            logger.error(f"[{cid:02d}] Error en inferencia: {e}", exc_info=True)
            continue
        elapsed = time.time() - t0

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
            logger.info(f"[{cid:02d}]({elapsed:.1f}s) {text[:80]}")
        stats[cid] = stats.get(cid, 0) + 1

        if time.time() - last_stats > 300:
            logger.info(f"Chunks: {stats} | VAD-skip: {vad_skipped}")
            last_stats = time.time()


if __name__ == "__main__":
    from multiprocessing import Queue
    run(Queue(), device="cuda")
