#!/usr/bin/env bash
# stop_all.sh — Detiene limpiamente todo el sistema de transcripción.
#
# Mata en orden:
#   1. run_alerts.py (AlertaTV web)
#   2. manager.py + workers FFmpeg + inference workers (todo el árbol del manager)
#   3. cualquier ffmpeg huérfano que haya quedado capturando streams
#   4. cualquier search_server.py huérfano (legacy)
#
# Uso:
#   bash stop_all.sh

set -u

GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${GREEN}[stop]${NC} $*"; }
warn() { echo -e "${YELLOW}[stop]${NC} $*"; }
err()  { echo -e "${RED}[stop]${NC} $*"; }

count_alive() {
    pgrep -f "python.*manager\.py|python.*run_alerts\.py|python.*search_server\.py|ffmpeg.*channelid" 2>/dev/null | wc -l
}

# ── 1. SIGTERM (limpio): manager primero (cascadeará a sus hijos vía daemon=True),
#       luego AlertaTV. Damos 10s para que cierren bien.
log "Enviando SIGTERM a todos los procesos del sistema..."
pkill -TERM -f "python.*run_alerts\.py"      2>/dev/null || true
pkill -TERM -f "python.*manager\.py"          2>/dev/null || true
pkill -TERM -f "python.*search_server\.py"    2>/dev/null || true

# Esperar hasta 10s a que terminen solos
for i in $(seq 1 10); do
    sleep 1
    n=$(count_alive)
    if [[ "$n" -eq 0 ]]; then
        log "Todos los procesos terminaron limpiamente (${i}s)"
        break
    fi
done

# ── 2. SIGKILL (forzado) si quedaron procesos
n=$(count_alive)
if [[ "$n" -gt 0 ]]; then
    warn "Quedaron $n procesos vivos — forzando SIGKILL..."
    pkill -KILL -f "python.*run_alerts\.py"      2>/dev/null || true
    pkill -KILL -f "python.*manager\.py"          2>/dev/null || true
    pkill -KILL -f "python.*search_server\.py"    2>/dev/null || true
    pkill -KILL -f "ffmpeg.*channelid"            2>/dev/null || true
    sleep 2
fi

# ── 3. ffmpeg huérfanos (sin proceso padre del manager): los matamos también
ffmpeg_left=$(pgrep -f "ffmpeg.*channelid" 2>/dev/null | wc -l)
if [[ "$ffmpeg_left" -gt 0 ]]; then
    warn "Aún hay $ffmpeg_left ffmpeg huérfanos — matando..."
    pkill -KILL -f "ffmpeg.*channelid" 2>/dev/null || true
    sleep 1
fi

# ── 4. Verificación final
final=$(count_alive)
if [[ "$final" -eq 0 ]]; then
    log "Sistema detenido limpiamente. 0 procesos restantes."
    exit 0
else
    err "ATENCIÓN: $final procesos siguen vivos. Listado:"
    pgrep -af "python.*manager\.py|python.*run_alerts\.py|python.*search_server\.py|ffmpeg.*channelid" || true
    exit 1
fi
