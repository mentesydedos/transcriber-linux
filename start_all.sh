#!/usr/bin/env bash
# start_all.sh — Arranca el sistema completo en orden correcto.
#
# Orden:
#   1. manager.py (transcripción 24x7) — toma ~45s en cargar el modelo y los 8 workers
#   2. run_alerts.py (AlertaTV web en :5001)
#
# Los logs van a logs/manager_stdout.log y logs/alerts_stdout.log.
#
# Uso:
#   bash start_all.sh           # arranque normal
#   bash start_all.sh --wait    # espera hasta que ambos respondan antes de salir

set -u

cd "$(dirname "$0")"

GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${GREEN}[start]${NC} $*"; }
warn() { echo -e "${YELLOW}[start]${NC} $*"; }
err()  { echo -e "${RED}[start]${NC} $*"; }

WAIT=false
[[ "${1:-}" == "--wait" ]] && WAIT=true

# ── Pre-checks ─────────────────────────────────────────────────────────────
if [[ ! -d venv ]]; then
    err "No existe ./venv — corre 'bash install.sh' primero"
    exit 1
fi

if pgrep -f "python.*manager\.py" >/dev/null 2>&1; then
    warn "manager.py ya está corriendo. Si quieres reiniciar, ejecuta primero: bash stop_all.sh"
    exit 1
fi

mkdir -p logs

# ── 1. Manager (transcripción) ─────────────────────────────────────────────
log "Arrancando manager.py (transcriber 24x7)..."
nohup ./venv/bin/python manager.py > logs/manager_stdout.log 2>&1 &
MGR_PID=$!
disown $MGR_PID
sleep 2

if ! kill -0 $MGR_PID 2>/dev/null; then
    err "manager.py murió inmediatamente. Revisa logs/manager_stdout.log"
    tail -20 logs/manager_stdout.log
    exit 1
fi
log "manager.py PID=$MGR_PID  → carga de modelos ~45s"

# ── 2. AlertaTV (panel web) ────────────────────────────────────────────────
log "Arrancando run_alerts.py (AlertaTV en :5001)..."
nohup ./venv/bin/python run_alerts.py > logs/alerts_stdout.log 2>&1 &
ALR_PID=$!
disown $ALR_PID
sleep 2

if ! kill -0 $ALR_PID 2>/dev/null; then
    err "run_alerts.py murió inmediatamente. Revisa logs/alerts_stdout.log"
    tail -20 logs/alerts_stdout.log
    exit 1
fi
log "run_alerts.py PID=$ALR_PID"

# ── 3. Espera opcional a que ambos respondan ───────────────────────────────
if $WAIT; then
    log "Esperando a que AlertaTV responda (max 60s)..."
    for i in $(seq 1 60); do
        if curl -s -o /dev/null --max-time 2 http://localhost:5001/login 2>/dev/null; then
            log "AlertaTV listo (${i}s)"
            break
        fi
        sleep 1
    done

    log "Esperando a que el primer chunk se transcriba (max 90s, indica que los 8 workers están vivos)..."
    for i in $(seq 1 90); do
        if [[ -f logs/transcriber.log ]] && grep -q "bs=" logs/transcriber.log 2>/dev/null; then
            log "Primer chunk transcrito (${i}s)"
            break
        fi
        sleep 1
    done
fi

# ── Resumen ────────────────────────────────────────────────────────────────
echo
log "Sistema arrancado:"
echo "  - manager.py        PID=$MGR_PID  (logs/manager_stdout.log, logs/transcriber.log)"
echo "  - run_alerts.py     PID=$ALR_PID  (logs/alerts_stdout.log)"
echo
log "Acceso web (red local):"
ip -4 addr show | grep -E "inet " | grep -v "127.0.0.1" | awk '{print "  http://" $2 "/"}' | sed 's|/[0-9]*||'
echo "  → AlertaTV en puerto 5001"
echo
log "Para detener: bash stop_all.sh"
log "Para ver logs: tail -f logs/manager.log logs/transcriber.log logs/alerts.log"
