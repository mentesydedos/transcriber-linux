#!/usr/bin/env bash
# install.sh — Instalador idempotente para Transcriber TV (Linux + Qwen3-ASR)
# Uso:
#   bash install.sh              # instalación completa
#   bash install.sh --no-model   # omite la descarga del modelo
#   bash install.sh --systemd    # además registra servicios systemd

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DOWNLOAD_MODEL=true
SETUP_SYSTEMD=false
for arg in "$@"; do
    case "$arg" in
        --no-model) DOWNLOAD_MODEL=false ;;
        --systemd)  SETUP_SYSTEMD=true ;;
        *) echo "Opción desconocida: $arg" >&2; exit 1 ;;
    esac
done

# Colores
GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}[install]${NC} $*"; }
warn() { echo -e "${YELLOW}[install]${NC} $*"; }
die()  { echo -e "${RED}[install]${NC} $*" >&2; exit 1; }

log "Directorio: $SCRIPT_DIR"

# ── 1. Verificar GPU NVIDIA ──────────────────────────────────────
log "Verificando driver NVIDIA..."
if ! command -v nvidia-smi >/dev/null 2>&1; then
    die "nvidia-smi no encontrado. Instala el driver: sudo apt install nvidia-driver-535"
fi
nvidia-smi -L || die "nvidia-smi falló — ¿GPU presente?"

# ── 2. Dependencias del sistema ──────────────────────────────────
log "Instalando dependencias del sistema (apt)..."
if ! command -v sudo >/dev/null 2>&1; then
    die "sudo no disponible — ejecuta como root o instala sudo"
fi
sudo apt update -qq
sudo apt install -y ffmpeg python3-venv python3-pip

# ── 3. Verificar ffmpeg ──────────────────────────────────────────
ffmpeg -version | head -1
ffprobe -version | head -1

# ── 4. Entorno virtual ───────────────────────────────────────────
if [[ ! -d venv ]]; then
    log "Creando entorno virtual en ./venv..."
    python3 -m venv venv
else
    log "Entorno virtual ya existe (./venv) — reutilizando"
fi

# shellcheck disable=SC1091
source venv/bin/activate

log "Actualizando pip..."
pip install --upgrade pip wheel setuptools

# ── 5. PyTorch CUDA ──────────────────────────────────────────────
if python -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
    log "PyTorch CUDA ya instalado — omitiendo"
else
    log "Instalando PyTorch con CUDA 12.1 (~2 GB)..."
    pip install torch --index-url https://download.pytorch.org/whl/cu121
fi

# ── 6. Resto de dependencias ─────────────────────────────────────
log "Instalando requirements.txt..."
pip install -r requirements.txt

# ── 7. Pre-descargar modelo Qwen3-ASR-1.7B ───────────────────────
if $DOWNLOAD_MODEL; then
    log "Pre-descargando Qwen3-ASR-1.7B (~3.4 GB, puede tardar varios minutos)..."
    mkdir -p models
    python <<'PY'
import os
os.environ["HF_HUB_ENABLE_HLTP_TRANSFER"] = "1"
from qwen_asr import Qwen3ASRModel
Qwen3ASRModel.from_pretrained("Qwen/Qwen3-ASR-1.7B", cache_dir="./models")
print("Modelo descargado OK")
PY
else
    warn "Saltando descarga del modelo (--no-model)"
fi

# ── 8. Carpetas de runtime ───────────────────────────────────────
mkdir -p logs output

# ── 9. Permisos ejecutables ──────────────────────────────────────
chmod +x install.sh 2>/dev/null || true

# ── 10. systemd (opcional) ───────────────────────────────────────
if $SETUP_SYSTEMD; then
    log "Registrando servicios systemd..."
    if [[ ! -d systemd ]]; then
        die "Carpeta ./systemd/ no encontrada"
    fi
    sudo cp systemd/transcriber.service /etc/systemd/system/
    sudo cp systemd/alerts.service      /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable transcriber.service alerts.service
    log "Servicios registrados. Para arrancarlos:"
    echo "  sudo systemctl start transcriber alerts"
    echo "  journalctl -u transcriber -f"
fi

# ── Cierre ───────────────────────────────────────────────────────
echo
echo -e "${BOLD}${GREEN}✓ Instalación completada${NC}"
echo
echo "Siguientes pasos:"
echo "  1. Editar TV\\ audio.m3u con tus streams"
echo "  2. Ejecutar:   source venv/bin/activate && python manager.py"
echo "  3. Dashboard:  python monitor.py (en otra terminal)"
echo "  4. Servicios:  bash install.sh --systemd  (para correr 24x7)"
echo
