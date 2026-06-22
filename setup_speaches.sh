#!/usr/bin/env bash
# setup_speaches.sh — A-to-Z setup for the Speaches (speech-to-text) service.
#
# Idempotent: safe to re-run. Ensures the model-cache volume exists, starts the
# Speaches container, pulls the Whisper model, and waits until the service
# answers — so when it finishes, the server can transcribe device audio.
#
# Usage:  bash setup_speaches.sh
set -euo pipefail

MODEL="Systran/faster-whisper-large-v3"
PORT=8001
VOLUME="hf-hub-cache"

info() { printf '\033[36m[speaches]\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m[speaches]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[speaches]\033[0m %s\n' "$*"; }

# 0. Docker up?
info "Checking Docker..."
docker info >/dev/null 2>&1 || { echo "Docker is not running. Start it and re-run."; exit 1; }

# 1. Model-cache volume (compose declares it external -> must pre-exist).
if docker volume ls --format '{{.Name}}' | grep -qx "$VOLUME"; then
    info "Model-cache volume '$VOLUME' already exists."
else
    info "Creating model-cache volume '$VOLUME'..."
    docker volume create "$VOLUME" >/dev/null
fi

# 2. Start the container.
info "Starting Speaches container..."
docker compose up -d speaches

# 3. Wait for the API.
info "Waiting for Speaches API on :$PORT ..."
ready=0
for _ in $(seq 1 60); do
    if curl -sf -m 3 "http://localhost:$PORT/v1/models" >/dev/null 2>&1; then ready=1; break; fi
    sleep 2
done
[ "$ready" = 1 ] || { echo "Speaches did not answer on :$PORT. Check 'docker logs speaches'."; exit 1; }
ok "API is up."

# 4. Ensure the model is present (pull once if missing).
info "Checking model '$MODEL' ..."
if curl -s "http://localhost:$PORT/v1/models" | grep -q "$MODEL"; then
    ok "Model already installed."
else
    info "Pulling model '$MODEL' (one-time download, ~1.5 GB)..."
    curl -s -m 600 -X POST "http://localhost:$PORT/v1/models/$MODEL" >/dev/null 2>&1 || \
        warn "Pull request errored; it may still be downloading."
    if curl -s "http://localhost:$PORT/v1/models" | grep -q "$MODEL"; then
        ok "Model installed."
    else
        warn "Model not yet listed. Open http://localhost:$PORT to pull via the UI, or re-run."
    fi
fi

# 5. Smoke test: transcribe 1s of silence (proves the STT path).
info "Smoke-testing transcription..."
tmp="$(mktemp).wav"
python - "$tmp" <<'PY' 2>/dev/null || true
import sys, wave
with wave.open(sys.argv[1], "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
    w.writeframes(b"\x00\x00" * 16000)
PY
if [ -f "$tmp" ]; then
    r=$(curl -s -m 60 "http://localhost:$PORT/v1/audio/transcriptions" \
        -F "file=@$tmp" -F "model=$MODEL" -F "response_format=json" || true)
    ok "Transcription endpoint responded: $r"
    rm -f "$tmp"
fi

ok "READY — Speaches is serving on http://localhost:$PORT (STT model: $MODEL)"
