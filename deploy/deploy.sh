#!/usr/bin/env bash
# Server-side deploy for line_art FastAPI app. Source is already rsynced by CI.
# NOTE: ComfyUI/speaches URLs in .env must be reachable from this server.
set -euo pipefail
cd /opt/line_art

echo "==> pull latest main"
git fetch origin main
git reset --hard origin/main

echo "==> python venv + deps"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "==> (re)start via pm2"
pm2 startOrReload /opt/ecosystem.config.js --only lineart --update-env
pm2 save
echo "==> line_art deploy done"
