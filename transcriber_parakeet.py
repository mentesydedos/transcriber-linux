"""
transcriber_parakeet.py — Worker de inferencia NVIDIA Parakeet-TDT-0.6B-v3 (GPU).

Reemplazo directo de transcriber.py (Qwen) para el pipeline: MISMA interfaz run(),
MISMA escritura a BD/archivos (dashboard, búsqueda, RAG y alertas lo consumen igual).
Corre bajo venv-parakeet (NeMo). No importa qwen_asr, así que es seguro en ese venv.

Elegido por env TRANSCRIBER_ENGINE=parakeet en manager.py. transcriber.py (Qwen)
queda intacto como rollback (TRANSCRIBER_ENGINE=qwen, el default).

Hallazgo que justifica la config (medido sobre TV mexicana real, jul-2026):
  - Parakeet en chunks de 30s reales → 0% code-switching, ~51x tiempo real en T1000.
  - Sobre silencio/música NO alucina (RNNT calla), a diferencia de modelos AED.
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
MODEL_NAME        = os.environ.get("TRANSCRIBER_PARAKEET_MODEL", "nvidia/parakeet-tdt-0.6b-v3")
MODEL_CACHE_DIR   = "./models"
SAMPLE_RATE       = 16000
GPU_BATCH_SIZE    = int(os.environ.get("TRANSCRIBER_PARAKEET_BATCH", "4"))
BATCH_WAIT_MS     = 150

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

# ── Base de datos (idéntica a transcriber.py) ─────────────────────────────────
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

# ── EPG / rotación de archivos (idéntico a transcriber.py) ────────────────────
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

# ── Inferencia Parakeet ───────────────────────────────────────────────────────
# Contexto izquierdo por canal (DESACTIVADO por defecto: no funcionó).
# La idea era anclar el idioma anteponiendo audio previo ya transcrito. MEDIDO
# sobre ADN 40 (noticias) el 2026-07-17: NO reduce el code-switching y hasta lo
# empeora (0s=24%, 60s=32%, 180s=30% de inglés) y pierde contenido. El drift de
# Parakeet en noticieros es sistemático y no se corrige con contexto. Se deja el
# mecanismo por si sirve en otro hardware/config, pero CONTEXT_SEC=0 (default).
CONTEXT_SEC = float(os.environ.get("TRANSCRIBER_PARAKEET_CONTEXT_SEC", "0"))

def _is_silent_or_noise(text: str) -> bool:
    if not text:
        return True
    cleaned = text.strip().strip(".,;:¿?¡!—-–…").lower()
    if not cleaned:
        return True
    markers = {"[música]", "[music]", "(música)", "(music)", "[silencio]", "[silence]"}
    return cleaned in markers

def transcribe_window(model, window_audio, ctx_dur: float) -> str:
    """Transcribe una ventana [contexto + nuevo] y devuelve SOLO el texto de la
    región nueva (segmentos con inicio >= ctx_dur). Devuelve '' si silencio/ruido.
    ctx_dur en segundos (0 = sin contexto → texto completo)."""
    hyp = model.transcribe([window_audio], batch_size=1, verbose=False,
                           timestamps=(ctx_dur > 0))[0]
    if ctx_dur <= 0:
        txt = (hyp.text if hasattr(hyp, "text") else str(hyp)).strip()
        return "" if _is_silent_or_noise(txt) else txt
    segs = getattr(hyp, "timestamp", None)
    segs = segs.get("segment") if isinstance(segs, dict) else None
    if not segs:
        # sin timestamps no podemos separar la región nueva; caemos al texto completo
        txt = (hyp.text if hasattr(hyp, "text") else str(hyp)).strip()
        return "" if _is_silent_or_noise(txt) else txt
    tol = 0.3  # un segmento que arranca justo antes del corte ya se emitió en el chunk previo
    parts = [(s.get("segment") or "").strip() for s in segs
             if (s.get("segment") or "").strip() and s.get("start", 0.0) >= ctx_dur - tol]
    txt = " ".join(parts).strip()
    return "" if _is_silent_or_noise(txt) else txt

# ── Loop principal (misma firma que transcriber.run) ──────────────────────────
def run(audio_queue, model_name: str = None, device: str = "cuda",
        worker_name: str = None, threads: int = None,
        batch_size: int = None, ready_event=None):
    import warnings; warnings.filterwarnings("ignore")
    import torch
    import nemo.collections.asr as nemo_asr

    if worker_name is None:
        worker_name = device
    if batch_size is None:
        batch_size = GPU_BATCH_SIZE
    model_id = model_name if (model_name and "parakeet" in str(model_name).lower()) else MODEL_NAME

    logger = setup_logger(worker_name)

    if device == "cuda" and not torch.cuda.is_available():
        logger.error("CUDA no disponible — worker Parakeet no puede arrancar")
        sys.exit(1)
    dev_name = torch.cuda.get_device_name(0) if device == "cuda" else "CPU"
    if device == "cuda":
        # TF32 en matmul: apagado por defecto en PyTorch aunque cudnn ya lo usa.
        # Gratis en Ampere/Ada (tensor cores) y sin costo de precisión relevante para ASR.
        torch.backends.cuda.matmul.allow_tf32 = True
    logger.info(f"Parakeet worker '{worker_name}' — device={device} ({dev_name}) batch={batch_size}")

    os.environ.setdefault("HF_HOME", os.path.abspath(MODEL_CACHE_DIR))
    logger.info(f"Cargando {model_id}...")
    model = nemo_asr.models.ASRModel.from_pretrained(model_name=model_id)
    model = model.to(device if device == "cuda" else "cpu")

    logger.info("Pre-calentando el modelo...")
    try:
        transcribe_window(model, np.zeros(SAMPLE_RATE, dtype=np.float32), 0.0)
    except Exception as e:
        logger.warning(f"Warmup falló (no crítico): {e}")

    ctx_samples = int(CONTEXT_SEC * SAMPLE_RATE)
    logger.info(f"Modelo listo — loop de inferencia (contexto={CONTEXT_SEC:.0f}s por canal)")
    if ready_event is not None:
        ready_event.set()

    file_windows = {}
    stats = {}
    context = {}   # channel_id -> np.ndarray con los últimos CONTEXT_SEC s de audio
    last_stats = time.time()

    # Se procesa un chunk a la vez (sin batch): con contexto cada ventana mide
    # ~CONTEXT_SEC+chunk; a ~51x rt un worker GPU cubre los 8 canales de sobra.
    while True:
        try:
            item = audio_queue.get(timeout=5)
        except Exception:
            if time.time() - last_stats > 300:
                logger.info(f"Idle | Chunks procesados: {stats}")
                last_stats = time.time()
            continue

        cid       = item["channel_id"]
        cname     = item["channel_name"]
        chunk     = item["audio"]
        chunk_sec = item.get("chunk_sec", 30)
        start_ts  = item.get("start_ts")
        end_ts    = item.get("end_ts")

        # Construir ventana [contexto + chunk nuevo]
        prev = context.get(cid) if ctx_samples > 0 else None
        if prev is not None and len(prev):
            window  = np.concatenate([prev, chunk])
            ctx_dur = len(prev) / SAMPLE_RATE
        else:
            window  = chunk
            ctx_dur = 0.0

        t0 = time.time()
        try:
            text = transcribe_window(model, window, ctx_dur)
        except Exception as e:
            logger.error(f"[{cid:02d}] Error en inferencia: {e}", exc_info=True)
            continue
        elapsed = time.time() - t0

        # Actualizar el contexto del canal: últimos CONTEXT_SEC s terminando aquí
        if ctx_samples > 0:
            merged = chunk if prev is None else np.concatenate([prev, chunk])
            context[cid] = merged[-ctx_samples:]

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
            logger.info(f"[{cid:02d}]({elapsed:.1f}s/ctx={ctx_dur:.0f}s) {text[:80]}")
        stats[cid] = stats.get(cid, 0) + 1

        if time.time() - last_stats > 300:
            logger.info(f"Chunks: {stats}")
            last_stats = time.time()


if __name__ == "__main__":
    from multiprocessing import Queue
    run(Queue(), device="cuda")
