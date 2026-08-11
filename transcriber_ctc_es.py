"""
transcriber_ctc_es.py — Worker de inferencia nvidia/parakeet-ctc-riva-0.6b
(español-inglés, decoder CTC) vía ONNX Runtime (CPU).

MISMA interfaz run() que transcriber_parakeet.py / transcriber_cohere.py: MISMA
escritura a BD/archivos (dashboard, búsqueda, RAG y alertas lo consumen igual).

Por qué existe: Parakeet-TDT deriva sistemáticamente al inglés en canales de
noticias (24-43% EN medido en ADN 40/N+/Canal 4). Cohere lo resolvía forzando
idioma, pero es un AED de 2B lento y necesita VAD porque alucina en silencio.
Este modelo (CTC, arquitectura frame-síncrona sin componente autorregresivo
fuerte) resultó 0% code-switching en pruebas A/B reales sobre los mismos 2
canales problemáticos (2026-08-10) -- ver el plan de esa fecha. CTC no
alucina sobre silencio (emite blank), así que tampoco hace falta VAD.

El modelo se distribuye en NGC como artefacto Riva (.riva = tar.gz con un
grafo ONNX + config + tokenizer), no como checkpoint .nemo ni en HuggingFace
-- por eso no se carga con nemo_asr.models.ASRModel.from_pretrained() como
los otros motores, sino con onnxruntime directo + el preprocesador de NeMo
(AudioToMelSpectrogramPreprocessor, reutilizado tal cual desde la config
exportada, para calzar exactamente los features que el grafo espera).

El .onnx original (model_graph.onnx, en models/parakeet-ctc-es/, no
versionado en git por tamaño) traía 24 nodos Split mal exportados
(ShapeInferenceError con onnxruntime moderno) -- se parcheó UNA vez
agregándoles el atributo num_outputs explícito y se guardó como
model_graph_fixed.onnx, que es el que se usa aquí.

Solo 3 canales (TRANSCRIBER_ONLY_CHANNELS): ADN 40, N+, Canal 4 -- CPU con
enorme margen (12.5x tiempo real medido con 4 hilos por chunk de 30s), no
hace falta GPU para este volumen.
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
MODEL_DIR         = Path(os.environ.get("TRANSCRIBER_CTC_ES_MODEL_DIR", "./models/parakeet-ctc-es"))
ONNX_FILE         = MODEL_DIR / "model_graph_fixed.onnx"
CONFIG_FILE       = MODEL_DIR / "model_config.yaml"
ONNX_THREADS      = int(os.environ.get("TRANSCRIBER_CTC_ES_THREADS", "4"))
SAMPLE_RATE       = 16000

FILE_WINDOW_MIN   = 30
DB_PATH           = os.environ.get("TRANSCRIBER_DB", "transcriptions.db")
ALERTS_DB         = Path("alerts.db")
# Ver misma nota en transcriber_parakeet.py -- split TV/radio por env, DB
# nunca se mueve del disco local. Este motor solo cubre TV (los 3 canales de
# noticias), así que en la práctica siempre cae en OUTPUT_DIR.
OUTPUT_DIR        = Path(os.environ.get("TRANSCRIBER_OUTPUT_DIR", "output"))
OUTPUT_DIR_RADIO  = Path(os.environ.get("TRANSCRIBER_OUTPUT_DIR_RADIO", str(OUTPUT_DIR)))
RADIO_CHANNEL_MIN = int(os.environ.get("TRANSCRIBER_RADIO_CHANNEL_MIN", "27"))
LOG_DIR           = Path("logs")


def _output_dir_for(channel_id: int) -> Path:
    return OUTPUT_DIR_RADIO if channel_id >= RADIO_CHANNEL_MIN else OUTPUT_DIR

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

# ── Base de datos (idéntica a transcriber_parakeet.py/transcriber_cohere.py) ──
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
        folder = _output_dir_for(self.channel_id) / (start.strftime("%Y-%m-%d_%H-%M") + end.strftime("_%H-%M"))
        folder.mkdir(parents=True, exist_ok=True)
        txt_path = folder / f"{self.base}.txt"
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

# ── Filtro de silencio/ruido (idéntico a transcriber_parakeet.py) ─────────────
def _is_silent_or_noise(text: str) -> bool:
    if not text:
        return True
    cleaned = text.strip().strip(".,;:¿?¡!—-–…").lower()
    if not cleaned:
        return True
    markers = {"[música]", "[music]", "(música)", "(music)", "[silencio]", "[silence]"}
    return cleaned in markers

# ── Inferencia: preprocesador NeMo + ONNX Runtime + CTC greedy decode ─────────
def _load_model(logger):
    import yaml
    import onnxruntime as ort
    from nemo.collections.asr.modules import AudioToMelSpectrogramPreprocessor

    cfg = yaml.safe_load(open(CONFIG_FILE))
    pp_cfg = dict(cfg["preprocessor"])
    pp_cfg.pop("_target_")
    preprocessor = AudioToMelSpectrogramPreprocessor(**pp_cfg)
    preprocessor.eval()

    vocab = cfg["decoder"]["vocabulary"]
    blank_id = len(vocab)  # convención NeMo/CTC: blank = último índice, fuera del vocabulario

    so = ort.SessionOptions()
    so.intra_op_num_threads = ONNX_THREADS
    session = ort.InferenceSession(str(ONNX_FILE), sess_options=so, providers=["CPUExecutionProvider"])
    logger.info(f"ONNX cargado: {ONNX_FILE.name} ({ONNX_THREADS} hilos), vocabulario={len(vocab)}")
    return preprocessor, session, vocab, blank_id


def _ctc_greedy_decode(logprobs: np.ndarray, vocab: list, blank_id: int) -> str:
    ids = np.argmax(logprobs, axis=-1)
    prev = None
    tokens = []
    for i in ids:
        if i != prev:
            if i != blank_id:
                tokens.append(vocab[i])
        prev = i
    return "".join(tokens).replace("▁", " ").strip()


def transcribe_chunk(preprocessor, session, vocab, blank_id, audio: np.ndarray) -> str:
    import torch
    audio_t  = torch.tensor(audio, dtype=torch.float32).unsqueeze(0)
    length_t = torch.tensor([audio_t.shape[1]], dtype=torch.int64)
    with torch.no_grad():
        feats, feat_len = preprocessor(input_signal=audio_t, length=length_t)
    out = session.run(["logprobs"], {
        "audio_signal": feats.numpy().astype(np.float32),
        "length": feat_len.numpy().astype(np.int64),
    })
    text = _ctc_greedy_decode(out[0][0], vocab, blank_id)
    return "" if _is_silent_or_noise(text) else text

# ── Loop principal (misma firma que transcriber_parakeet.run / transcriber_cohere.run) ─
def run(audio_queue, model_name: str = None, device: str = "cpu",
        worker_name: str = None, threads: int = None,
        batch_size: int = None, ready_event=None):
    import warnings; warnings.filterwarnings("ignore")

    if worker_name is None:
        worker_name = device
    logger = setup_logger(worker_name)
    logger.info(f"CTC-ES worker '{worker_name}' — device=cpu (ONNX Runtime)")

    logger.info("Cargando parakeet-ctc-es (ONNX)...")
    preprocessor, session, vocab, blank_id = _load_model(logger)

    logger.info("Pre-calentando el modelo...")
    try:
        transcribe_chunk(preprocessor, session, vocab, blank_id, np.zeros(SAMPLE_RATE, dtype=np.float32))
    except Exception as e:
        logger.warning(f"Warmup falló (no crítico): {e}")

    logger.info("Modelo listo — loop de inferencia")
    if ready_event is not None:
        ready_event.set()

    file_windows = {}
    stats = {}
    last_stats = time.time()

    while True:
        try:
            item = audio_queue.get(timeout=5)
        except Exception:
            if time.time() - last_stats > 300:
                logger.info(f"Idle | Chunks: {stats}")
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
            text = transcribe_chunk(preprocessor, session, vocab, blank_id, chunk)
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
            logger.info(f"Chunks: {stats}")
            last_stats = time.time()


if __name__ == "__main__":
    from multiprocessing import Queue
    run(Queue(), device="cpu")
