"""
music_classifier_client.py — Cliente liviano para hablarle al servidor
aislado de detección de música (music_classifier_server.py, venv-music,
servicio music-classifier.service).

A propósito solo usa stdlib (socket/struct) + numpy -- CERO dependencia de
onnxruntime -- así se puede importar sin riesgo desde los motores de
transcripción (transcriber_ctc_es.py, transcriber_parakeet.py), que corren
en venv-parakeet junto con onnxruntime-gpu. Ver la nota en
music_classifier_server.py sobre el incidente que motivó separar esto en
un proceso/venv aparte.

Cualquier falla (servidor caído, tardado, socket no existe) devuelve
has_music=False en silencio -- nunca debe frenar ni tronar la transcripción
en vivo por esto, igual que el fallback original en music_classifier.py.
"""
import logging
import os
import socket
import struct
from pathlib import Path

import numpy as np

BASE_DIR = Path(__file__).parent
SOCK_PATH = str(BASE_DIR / "music_classifier.sock")
# Generoso a propósito: inferencia real ~100ms, pero el servidor serializa
# solicitudes (un lock) y puede haber ráfagas (todos los canales
# reconectando al arrancar un servicio) -- mejor esperar un poco que
# empezar a marcar has_music=False de más por impaciencia.
TIMEOUT = float(os.environ.get("MUSIC_CLASSIFIER_CLIENT_TIMEOUT", "3.0"))
THRESHOLD = float(os.environ.get("MUSIC_CLASSIFIER_THRESHOLD", "0.5"))

log = logging.getLogger("music_classifier_client")
_warned = False  # avisa una sola vez si el servidor no responde, no en cada chunk/canal (inundaría el log cada 30s x ~90 canales)


def _recv_exact(conn: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def music_score(audio: np.ndarray) -> float | None:
    """None si no se pudo consultar (servidor caído/tardado/sin responder)
    -- distinto de 0.0 (sí respondió y no detectó música), para que el
    llamador decida el fallback."""
    global _warned
    payload = np.ascontiguousarray(audio, dtype=np.float32).tobytes()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT)
            s.connect(SOCK_PATH)
            s.sendall(struct.pack(">I", len(payload)) + payload)
            resp = _recv_exact(s, 4)
        if resp is None:
            raise ConnectionError("respuesta incompleta del servidor")
        (score,) = struct.unpack(">f", resp)
        _warned = False
        return score
    except Exception as e:
        if not _warned:
            log.warning("music-classifier-server no disponible (%s) -- has_music=False hasta que se reconecte", e)
            _warned = True
        return None


def is_music(audio: np.ndarray, threshold: float = THRESHOLD) -> bool:
    score = music_score(audio)
    return score is not None and score >= threshold
