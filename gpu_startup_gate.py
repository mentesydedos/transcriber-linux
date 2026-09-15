#!/usr/bin/env python3
"""
gpu_startup_gate.py — ExecStartPre de transcriber-parakeet.service (radio).

Mismo principio que start_and_verify_gpu_engine() en startup_sequence.py
(TV sano ANTES que radio) pero para un (re)arranque AD HOC durante
operación normal: si ambos motores de GPU se caen casi al mismo tiempo,
Restart=always de systemd los reinicia a los dos SIN coordinación entre
unidades -- el mismo patrón que causó el incidente de OOM de esta sesión,
esta vez disparado por una caída en vivo en vez de un arranque de máquina.

Un vigilante externo que sondea cada N segundos no alcanza a interceptar la
carrera (RestartSec=10 es más rápido que cualquier intervalo de sondeo
razonable). Este script en cambio se engancha en el momento exacto en que
systemd va a arrancar radio y sencillamente no deja pasar hasta confirmar
que TV está sano -- reutilizando el mismo criterio que ya usa
system_health.py (activo Y con transcripción reciente, no solo "systemctl
dice que está activo", que fue justo lo que falló en el incidente).

Si TV no queda sano dentro de GATE_TIMEOUT_SEC, sale con código 1 -- eso
hace fallar el ExecStartPre, lo que hace fallar el arranque de
transcriber-parakeet.service completo, y systemd lo reintenta solo según su
propia política ya existente (Restart=always, RestartSec=10). No se pelea
con systemd: se apoya en su mecanismo de reintentos.

TV nunca espera a radio (mismo orden de prioridad que startup_sequence.py)
-- solo transcriber-parakeet.service lleva este gate.
"""
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from system_health import is_active, latest_transcription_ts

TV_UNIT = "transcriber-ctc-es.service"
TV_CHANNEL_MIN, TV_CHANNEL_MAX = 1, 26
# Igual a ENGINE_WARMUP_SEC en startup_sequence.py -- si TV tampoco está
# sano después de esto, mejor fallar rápido (y dejar que el próximo ciclo
# de Restart=always de radio lo reintente) que bloquear indefinidamente.
GATE_TIMEOUT_SEC = 100
POLL_SEC = 5
# "Reciente" = 2x el tamaño de chunk de TV (30s) -- chequeo rápido y
# repetido, sin necesidad de una ventana antes/después como
# system_health.check_gpu_engine (eso sumaría GATE_TIMEOUT_SEC/POLL_SEC
# ventanas de 12s cada una, innecesario para un gate).
RECENT_SEC = 60


def tv_healthy() -> bool:
    if not is_active(TV_UNIT):
        return False
    ts = latest_transcription_ts(TV_CHANNEL_MIN, TV_CHANNEL_MAX)
    if not ts:
        return False
    try:
        age = (datetime.now() - datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")).total_seconds()
    except ValueError:
        return False
    return age <= RECENT_SEC


def main() -> int:
    deadline = time.time() + GATE_TIMEOUT_SEC
    while time.time() < deadline:
        if tv_healthy():
            return 0
        time.sleep(POLL_SEC)
    print(f"[gpu_startup_gate] TV ({TV_UNIT}) no está sano tras {GATE_TIMEOUT_SEC}s -- "
          f"bloqueando arranque de radio para no repetir el incidente de OOM.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
