"""
worker.py — Captura de audio de un canal M3U.

Cada worker tiene su PROPIA cola de salida (maxsize=3) con drop-oldest:
si los inference workers no dan abasto, el chunk más viejo se descarta en
vez de bloquear. Esto impide que un canal atrasado congele al sistema.
"""

import os
import subprocess
import numpy as np
import sqlite3
import time
import sys
import logging
import threading
import queue
from datetime import datetime, timedelta
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
SAMPLE_RATE     = 16000
# Largo del chunk, configurable por env. 30s es el contexto nativo de Whisper/Qwen.
# Parakeet en cambio deriva al inglés en registros formales (noticieros) con chunks
# cortos aislados; una ventana más larga (60-120s) le da contexto y lo ancla en
# español. Ver TRANSCRIBER_CHUNK_SEC en el servicio de Parakeet.
CHUNK_SECONDS   = int(os.environ.get("TRANSCRIBER_CHUNK_SEC", "30"))
CHUNK_SAMPLES   = SAMPLE_RATE * CHUNK_SECONDS
OVERLAP_SEC     = 0              # sin overlap: chunks de 30s son suficientemente largos
OVERLAP_SAMPLES = int(SAMPLE_RATE * OVERLAP_SEC)

# DB configurable por env (para pruebas sin tocar la de producción).
DB_PATH = os.environ.get("TRANSCRIBER_DB", "transcriptions.db")
LOG_DIR = Path("logs")

# CC-first: si llegó un caption en los últimos CC_GRACE_SEC, NO se manda audio a
# Qwen (el CC ya cubre ese tramo). Ahorra cómputo de ASR en canales con CC.
CC_GRACE_SEC = float(os.environ.get("TRANSCRIBER_CC_GRACE", "90"))
# Poner TRANSCRIBER_CC=0 para desactivar la rama de Closed Captions.
CC_ENABLED = os.environ.get("TRANSCRIBER_CC", "1").lower() not in ("0", "false", "no")

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
    # Origen de la transcripción: 'asr' (Qwen3) o 'cc' (ccextractor). Default 'asr'
    # para no romper las 969k filas históricas.
    tcols = {r[1] for r in conn.execute("PRAGMA table_info(transcriptions)").fetchall()}
    if "source" not in tcols:
        conn.execute("ALTER TABLE transcriptions ADD COLUMN source TEXT DEFAULT 'asr'")
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
def _header_args(headers: str | None) -> list:
    """-headers de ffmpeg/ffprobe: crudo, terminado en \\r\\n. Necesario para
    estaciones detrás de Zeno.fm, que rechazan con 401 sin un Origin/Referer
    del sitio autorizado (ver #EXTHEADER en manager.py:parse_m3u)."""
    if not headers:
        return []
    return ["-headers", headers.rstrip("\r\n") + "\r\n"]


def detect_video(url: str, logger: logging.Logger, headers: str | None = None) -> bool:
    """True si la fuente trae un stream de video (canales de TV vía TVHeadend) —
    False para fuentes de solo audio (radio FM por internet). Evita que
    cc_extractor_thread intente (y falle, en loop cada 3s para siempre) extraer
    Closed Captions de un stream que nunca tuvo video."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", *_header_args(headers),
             "-select_streams", "v:0",
             "-show_entries", "stream=codec_type",
             "-of", "default=noprint_wrappers=1:nokey=1", url],
            capture_output=True, text=True, timeout=15)
        return result.stdout.strip() == "video"
    except Exception as e:
        logger.warning(f"No se pudo detectar video ({e}), asumiendo que sí trae")
        return True


def detect_channels(url: str, logger: logging.Logger, headers: str | None = None) -> int:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", *_header_args(headers),
             "-select_streams", "a:0",
             "-show_entries", "stream=channels",
             "-of", "default=noprint_wrappers=1:nokey=1", url],
            capture_output=True, text=True, timeout=15)
        n = int(result.stdout.strip())
        logger.info(f"Stream: {n} canales de audio")
        return n
    except Exception as e:
        logger.warning(f"No se pudo detectar canales ({e}), asumiendo estéreo")
        return 2

def start_ffmpeg(url: str, logger: logging.Logger, channels: int = 2,
                 headers: str | None = None) -> subprocess.Popen:
    af_filter = ["-af", "pan=mono|c0=FC"] if channels > 2 else ["-ac", "1"]
    # La fuente ahora es un MPEG-TS local (sintonizador), con video+audio del
    # subcanal. Dejamos que ffmpeg detecte el contenedor (sin `-f ac3`),
    # descartamos el video (-vn) y tomamos el primer audio.
    cmd = [
        "ffmpeg",
        "-reconnect", "1", "-reconnect_streamed", "1",
        "-reconnect_delay_max", "3", "-reconnect_at_eof", "1",
        "-timeout", "8000000",
        "-fflags", "nobuffer", "-flags", "low_delay",
        *_header_args(headers),
        "-i", url,
        "-vn", "-map", "0:a:0",
        "-acodec", "pcm_s16le", "-ar", str(SAMPLE_RATE),
    ] + af_filter + ["-f", "s16le", "pipe:1", "-loglevel", "warning"]
    logger.info(f"FFmpeg iniciado: {url[:60]}... (ch={channels})")
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=CHUNK_SAMPLES * 2)


# ── Closed Captions (CC-first) ──────────────────────────────────────────────────
def _tc_to_ms(tc: str) -> int:
    h, m, rest = tc.strip().split(":")
    s, ms = rest.split(",")
    return (int(h) * 3600 + int(m) * 60 + int(s)) * 1000 + int(ms)


def _cc_block_duration(tcline: str) -> float:
    try:
        a, b = tcline.split(" --> ", 1)
        return max(0.0, (_tc_to_ms(b) - _tc_to_ms(a)) / 1000.0)
    except (ValueError, IndexError):
        return 0.0


def cc_extractor_thread(channel_id: int, channel_name: str, url: str,
                        cc_state: dict, stop_event: threading.Event,
                        logger: logging.Logger):
    """Corre ccextractor sobre el TS del subcanal. Cada caption se escribe en la
    DB con source='cc' y actualiza cc_state['last'] (para que el loop de audio
    saltee Qwen mientras haya CC). Auto-reinicia si el pipe cae."""
    # Import diferido (evita ciclo al cargar) del motor activo: cada uno trae su
    # propia save_to_db/FileWindow, y transcriber.py (Qwen) exige qwen_asr, que
    # no existe en venv-parakeet/venv-cohere.
    _engine = os.environ.get("TRANSCRIBER_ENGINE", "qwen").lower()
    if _engine == "parakeet":
        from transcriber_parakeet import save_to_db, FileWindow
    elif _engine == "cohere":
        from transcriber_cohere import save_to_db, FileWindow
    else:
        from transcriber import save_to_db, FileWindow
    # Perezosa: solo se crea (y escribe su header) si este canal de verdad trae
    # CC — la mayoría no. Instancia propia de este hilo (proceso del canal); el
    # inference worker (otro proceso) tiene la suya para el audio. Ambas
    # escriben al mismo TXT/SRT del bloque; en la práctica no compiten porque
    # CC-first hace que solo una fuente esté activa a la vez para un mismo
    # tramo de tiempo.
    file_window = None
    last_end_dt = None   # evita que un bloque nuevo empiece antes de que termine el previo
    while not stop_event.is_set():
        ff = cc = None
        try:
            ff = subprocess.Popen(
                ["ffmpeg", "-reconnect", "1", "-reconnect_streamed", "1",
                 "-reconnect_delay_max", "3", "-reconnect_at_eof", "1",
                 "-timeout", "8000000", "-i", url,
                 "-map", "0:v:0", "-c", "copy", "-f", "mpegts", "pipe:1",
                 "-loglevel", "error"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            cc = subprocess.Popen(
                ["ccextractor", "-quiet", "-s", "-1",
                 "-in=ts", "-stdin", "-out=srt", "-stdout",
                 "--nofontcolor", "--norollup"],
                stdin=ff.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace")
            block = []
            for line in cc.stdout:
                if stop_event.is_set():
                    break
                line = line.rstrip("\r\n")
                if line == "":
                    if len(block) >= 3 and " --> " in block[1]:
                        text = " ".join(t.strip() for t in block[2:] if t.strip())
                        while "  " in text:
                            text = text.replace("  ", " ")
                        if text:
                            cc_state["last"] = time.time()
                            dur = _cc_block_duration(block[1])
                            end_dt = datetime.now()
                            start_dt = end_dt - timedelta(seconds=dur)
                            if last_end_dt is not None and start_dt < last_end_dt:
                                start_dt = min(last_end_dt, end_dt)
                            last_end_dt = end_dt
                            now = end_dt.isoformat(sep=" ", timespec="milliseconds")
                            try:
                                save_to_db(channel_id, channel_name, text,
                                           dur, start_ts=now, source="cc")
                            except Exception as e:
                                logger.error(f"CC save_to_db: {e}")
                            try:
                                if file_window is None:
                                    file_window = FileWindow(channel_id, channel_name)
                                file_window.write(
                                    start_dt.isoformat(sep=" ", timespec="milliseconds"),
                                    now, text)
                            except Exception as e:
                                logger.error(f"CC escribiendo archivo: {e}")
                    block = []
                else:
                    block.append(line)
        except Exception as e:
            logger.warning(f"CC extractor error: {e}")
        finally:
            for p in (cc, ff):
                if p and p.poll() is None:
                    try:
                        p.terminate()
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try: p.kill()
                        except Exception: pass
                    except Exception:
                        pass
        if not stop_event.is_set():
            time.sleep(3)

def ffmpeg_reader(proc: subprocess.Popen, audio_q: queue.Queue,
                  stop_event: threading.Event, logger: logging.Logger):
    """Lee audio de FFmpeg y empuja tuplas (audio, end_ts) a la cola.
    end_ts es el reloj de pared al momento en que el chunk está completo — usado
    más adelante para calcular start_ts = end_ts - CHUNK_SECONDS y anclar la
    transcripción al tiempo real del stream (no al momento de inferencia)."""
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
                audio_q.put((audio, datetime.now()))
            remainder = raw[n * CHUNK_SAMPLES * 2:]
        except Exception as e:
            logger.error(f"Error leyendo FFmpeg: {e}")
            break

# ── Loop principal ─────────────────────────────────────────────────────────────
def run_worker(channel_id: int, channel_name: str, url: str, shared_queue,
               headers: str | None = None):
    logger = setup_logger(channel_id)
    logger.info(f"Worker iniciado | canal={channel_name}")
    init_db()
    update_status(channel_id, channel_name, url, "running")

    hb_stop   = threading.Event()
    hb_thread = threading.Thread(target=send_heartbeat, args=(channel_id, hb_stop), daemon=True)
    hb_thread.start()

    # CC-first: hilo que extrae Closed Captions del subcanal. Mientras haya CC
    # reciente, el loop de audio no manda chunks a Qwen (ahorra ASR). Solo tiene
    # sentido si la fuente trae video (TV) — una radio de solo audio nunca va a
    # tener CC, así que ni se intenta (evita un loop de reintento cada 3s para
    # siempre por canal, sin ningún beneficio).
    cc_state = {"last": 0.0}
    cc_stop = threading.Event()
    if CC_ENABLED and detect_video(url, logger, headers):
        cc_thread = threading.Thread(
            target=cc_extractor_thread,
            args=(channel_id, channel_name, url, cc_state, cc_stop, logger),
            daemon=True)
        cc_thread.start()
        logger.info("CC-first activo (grace=%.0fs)", CC_GRACE_SEC)
    elif CC_ENABLED:
        logger.info("Sin stream de video (fuente de solo audio) — CC-first desactivado")

    audio_channels = detect_channels(url, logger, headers)
    previous_audio = np.zeros(OVERLAP_SAMPLES, dtype=np.float32)
    reconnect_delay = 2
    max_reconnects  = 999

    for attempt in range(max_reconnects):
        if attempt > 0:
            logger.info(f"Reconectando en {reconnect_delay}s (intento {attempt})...")
            time.sleep(reconnect_delay)

        proc       = start_ffmpeg(url, logger, channels=audio_channels, headers=headers)
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
                    chunk, end_ts = local_q.get(timeout=60)
                except queue.Empty:
                    logger.warning("Timeout esperando audio — stream posiblemente caído")
                    break

                audio_with_overlap = np.concatenate([previous_audio, chunk]) if OVERLAP_SAMPLES > 0 else chunk
                previous_audio     = chunk[-OVERLAP_SAMPLES:] if OVERLAP_SAMPLES > 0 else np.zeros(0, dtype=np.float32)

                # Timestamps absolutos del audio (anclados al reloj de pared en captura,
                # NO al momento de inferencia). Formato ISO con milisegundos.
                start_ts = (end_ts - timedelta(seconds=CHUNK_SECONDS)).isoformat(
                    sep=" ", timespec="milliseconds")
                end_ts_s = end_ts.isoformat(sep=" ", timespec="milliseconds")

                # CC-first: si hubo Closed Captions hace poco, ese tramo ya quedó
                # transcripto vía ccextractor → no gastamos Qwen en este chunk.
                if CC_ENABLED and (time.time() - cc_state["last"]) < CC_GRACE_SEC:
                    continue

                item = {
                    "channel_id":   channel_id,
                    "channel_name": channel_name,
                    "audio":        audio_with_overlap,
                    "chunk_sec":    CHUNK_SECONDS,
                    "start_ts":     start_ts,
                    "end_ts":       end_ts_s,
                }

                # Drop-oldest: si la cola está llena, descartar el chunk más viejo
                # antes de meter el nuevo. Evita que un canal atrasado bloquee.
                while True:
                    try:
                        shared_queue.put_nowait(item)
                        break
                    except queue.Full:
                        try:
                            shared_queue.get_nowait()
                            logger.warning("Cola llena — chunk viejo descartado")
                        except queue.Empty:
                            pass

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Error en loop: {e}", exc_info=True)
        finally:
            stop_event.set()
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("ffmpeg no respondió a terminate() en 5s — kill()")
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    logger.error("ffmpeg no murió ni con kill() — posible zombie")
            reader.join(timeout=3)

    hb_stop.set()
    cc_stop.set()
    logger.info("Worker terminado")


if __name__ == "__main__":
    # Modo legado: ejecución directa sin cola compartida (para pruebas)
    import sys
    if len(sys.argv) != 5:
        print("Uso: python worker.py <id> <nombre> <url> <device>")
        sys.exit(1)
    print("AVISO: Ejecutar via manager.py para usar el transcriber centralizado")
    sys.exit(1)
