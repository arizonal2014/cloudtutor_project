#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8080}"
FRONTEND_PORT="${FRONTEND_PORT:-4173}"

if [[ ! -x ".venv/bin/uvicorn" ]]; then
  echo "Missing .venv dependencies. Activate/setup .venv first."
  exit 1
fi

echo "Starting CloudTutor Session 01 dev servers..."
echo "Backend:  http://${BACKEND_HOST}:${BACKEND_PORT}"
echo "Frontend: http://127.0.0.1:${FRONTEND_PORT}"

# Avoid stale reload processes serving old code paths.
pkill -f "uvicorn backend.app.main:app" >/dev/null 2>&1 || true
pkill -f "python -m http.server ${FRONTEND_PORT}" >/dev/null 2>&1 || true

.venv/bin/uvicorn backend.app.main:app --reload --host "$BACKEND_HOST" --port "$BACKEND_PORT" &
BACKEND_PID=$!

.venv/bin/python -m http.server "$FRONTEND_PORT" --bind 127.0.0.1 --directory frontend &
FRONTEND_PID=$!

cleanup() {
  if kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
  if kill -0 "$FRONTEND_PID" 2>/dev/null; then
    kill "$FRONTEND_PID" 2>/dev/null || true
    wait "$FRONTEND_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# macOS ships an older bash without `wait -n`, so we poll both PIDs.
while true; do
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    wait "$BACKEND_PID" 2>/dev/null || true
    exit 1
  fi
  if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
    wait "$FRONTEND_PID" 2>/dev/null || true
    exit 1
  fi
  sleep 1
done
