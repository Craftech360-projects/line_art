#!/usr/bin/env bash
# Server-side deploy for line_art FastAPI app. Source is already rsynced/pulled by CI.
# Health-gates the reload and auto-rolls-back to the previous commit on failure.
set -euo pipefail
cd /opt/line_art

PORT="${PORT:-8090}"
PREV_SHA="$(git rev-parse HEAD)"

echo "==> pull latest main (prev=$PREV_SHA)"
git fetch origin main
git reset --hard origin/main

reload() {
  echo "==> venv + deps"
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
  echo "==> pm2 reload"
  pm2 startOrReload /opt/ecosystem.config.js --only lineart --update-env
  pm2 save
}

healthy() {
  for i in $(seq 1 15); do
    if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

reload
if healthy; then
  echo "==> line_art deploy healthy"
else
  echo "!! health check FAILED — rolling back to $PREV_SHA"
  git reset --hard "$PREV_SHA"
  reload
  healthy && echo "==> rolled back to previous healthy revision" || echo "!! rollback also unhealthy — manual intervention needed"
  exit 1
fi
