#!/usr/bin/env bash
# setup_comfyui.sh — A-to-Z setup for the ComfyUI (image generation) service.
#
# Idempotent: safe to re-run. Creates the model dirs, builds the local ComfyUI
# image, downloads the FLUX.1-schnell fp8 checkpoint (~17 GB, resumable), starts
# the container, and waits until ComfyUI sees the checkpoint — so when it
# finishes, the server can generate line art.
#
# Usage:  bash setup_comfyui.sh
set -euo pipefail

PORT=8188
CKPT="flux1-schnell-fp8.safetensors"
CKPT_URL="https://huggingface.co/Comfy-Org/flux1-schnell/resolve/main/flux1-schnell-fp8.safetensors"
EXPECTED_BYTES=17236328572   # ~17.2 GB
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$SCRIPT_DIR/comfyui-data"
CKPT_DIR="$DATA_DIR/models/checkpoints"
CKPT_PATH="$CKPT_DIR/$CKPT"

info() { printf '\033[36m[comfyui]\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m[comfyui]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[comfyui]\033[0m %s\n' "$*"; }

# 0. Docker up?
info "Checking Docker..."
docker info >/dev/null 2>&1 || { echo "Docker is not running. Start it and re-run."; exit 1; }

# 1. Data dirs (match the compose bind mounts).
info "Ensuring model directories under $DATA_DIR ..."
mkdir -p "$CKPT_DIR" "$DATA_DIR/models/vae" "$DATA_DIR/output"

# 2. Download the FLUX checkpoint (resumable; skip if complete).
cur=0; [ -f "$CKPT_PATH" ] && cur=$(stat -c %s "$CKPT_PATH" 2>/dev/null || stat -f %z "$CKPT_PATH")
if [ "$cur" = "$EXPECTED_BYTES" ]; then
    ok "Checkpoint already present and complete (~17.2 GB)."
else
    [ "$cur" -gt 0 ] 2>/dev/null && info "Resuming checkpoint download..." || info "Downloading FLUX checkpoint (~17 GB; takes a while)..."
    curl -L -C - --retry 5 --retry-delay 5 -o "$CKPT_PATH" "$CKPT_URL"
    now=$(stat -c %s "$CKPT_PATH" 2>/dev/null || stat -f %z "$CKPT_PATH")
    if [ "$now" != "$EXPECTED_BYTES" ]; then
        warn "Downloaded size $now != expected $EXPECTED_BYTES. Re-run to resume."
        exit 1
    fi
    ok "Checkpoint downloaded and size-verified."
fi

# 3. Build the image (idempotent; uses cache).
info "Building ComfyUI image (first run installs ComfyUI; later runs cached)..."
docker compose build comfyui

# 4. Start the container.
info "Starting ComfyUI container..."
docker compose up -d comfyui

# 5. Wait for the API.
info "Waiting for ComfyUI API on :$PORT ..."
ready=0
for _ in $(seq 1 60); do
    if curl -sf -m 3 "http://localhost:$PORT/" >/dev/null 2>&1; then ready=1; break; fi
    sleep 2
done
[ "$ready" = 1 ] || { echo "ComfyUI did not answer on :$PORT. Check 'docker logs comfyui'."; exit 1; }
ok "API is up."

# 6. Confirm the checkpoint is visible.
info "Verifying the checkpoint is visible to ComfyUI..."
if curl -s "http://localhost:$PORT/object_info/CheckpointLoaderSimple" | grep -q "$CKPT"; then
    ok "Checkpoint '$CKPT' is loaded and selectable."
else
    warn "Checkpoint not listed yet. If you just added it: docker compose restart comfyui"
fi

# 7. GPU check inside the container.
info "Checking GPU access in the container..."
gpu=$(docker exec comfyui python -c "import torch;print('cuda',torch.cuda.is_available(),torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')" 2>/dev/null || true)
case "$gpu" in
    *"cuda True"*) ok "GPU OK: $gpu" ;;
    *) warn "GPU not detected inside container: $gpu" ;;
esac

ok "READY — ComfyUI is serving on http://localhost:$PORT (model: $CKPT)"
warn "Note: the FIRST generation loads the 17 GB model into VRAM (a few minutes); later ones are ~5 s."
