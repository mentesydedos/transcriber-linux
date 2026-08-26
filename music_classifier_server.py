"""
music_classifier_server.py — Servidor aislado de detección de música por
audio (YAMNet), para no volver a poner en riesgo la GPU de transcripción.

Antecedente (incidente 2026-08-26): music_classifier.py se probó importado
directo dentro de los motores de transcripción (venv-parakeet). Aunque pide
CPUExecutionProvider explícito, ese venv tiene onnxruntime-gpu instalado
(lo necesita Parakeet/CTC-ES) -- y el mismo día el motor de radio entró en
OOM de CUDA justo después de activar el clasificador ahí. No se pudo
descartar con certeza que el paquete GPU, con solo inicializarse en el
proceso, reservara contexto CUDA sin importar el provider pedido.

Este servidor corre en SU PROPIO venv (venv-music), que a propósito no
tiene ni onnxruntime-gpu ni ningún paquete nvidia-* instalado -- no existe
código aquí que pueda tocar la GPU, ni por accidente. Los motores de
transcripción (transcriber_ctc_es.py, transcriber_parakeet.py) le hablan
por un socket Unix local -- ver music_classifier_client.py -- sin compartir
proceso, venv, ni importar nada de este archivo.

Si este proceso se cae, se satura o tarda, los motores de transcripción NO
se bloquean ni truenan: el cliente falla en silencio (has_music=False) y la
transcripción en vivo sigue sin marcar música -- ese es el fallback seguro,
igual que cuando el clasificador corría en proceso.
"""
import logging
import os
import socket
import struct
import threading
from pathlib import Path

import numpy as np

import music_classifier as mc

BASE_DIR = Path(__file__).parent
SOCK_PATH = str(BASE_DIR / "music_classifier.sock")
MAX_PAYLOAD = 64 * 1024 * 1024  # ~10x un chunk de 30s a 16kHz float32 -- tope generoso contra solicitudes corruptas

logging.basicConfig(level=logging.INFO, format="%(asctime)s [music-classifier] %(levelname)s: %(message)s")
log = logging.getLogger("music_classifier_server")

# Una inferencia a la vez: YAMNet ya es rápido (~100ms/chunk) y el volumen
# real es bajo (~90 canales cada 30s en promedio) -- serializar evita
# sobresuscribir CPU si varios canales piden casi al mismo tiempo (p.ej. al
# arrancar el servicio y reconectar todos los canales en ráfaga), sin
# necesitar un pool ni medir contención de onnxruntime entre hilos.
_lock = threading.Lock()


def _recv_exact(conn: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _handle(conn: socket.socket) -> None:
    try:
        conn.settimeout(5.0)
        header = _recv_exact(conn, 4)
        if header is None:
            return
        (length,) = struct.unpack(">I", header)
        if length <= 0 or length > MAX_PAYLOAD:
            log.warning("Longitud de payload fuera de rango: %d", length)
            return
        payload = _recv_exact(conn, length)
        if payload is None:
            return
        audio = np.frombuffer(payload, dtype=np.float32)
        with _lock:
            score = mc.music_score(audio)
        conn.sendall(struct.pack(">f", score))
    except Exception as e:
        log.warning("Error atendiendo solicitud: %s", e)
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main() -> None:
    mc._load()  # carga el modelo una sola vez al iniciar, no en la primera solicitud
    log.info("Modelo YAMNet cargado (CPUExecutionProvider), escuchando en %s", SOCK_PATH)

    if os.path.exists(SOCK_PATH):
        os.remove(SOCK_PATH)  # socket huérfano de una caída anterior -- bind() falla si no se limpia primero

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o666)
    server.listen(64)

    try:
        while True:
            conn, _ = server.accept()
            threading.Thread(target=_handle, args=(conn,), daemon=True).start()
    finally:
        server.close()
        if os.path.exists(SOCK_PATH):
            os.remove(SOCK_PATH)


if __name__ == "__main__":
    main()
