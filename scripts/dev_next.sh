#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8080}"
FRONTEND_NEXT_HOST="${FRONTEND_NEXT_HOST:-127.0.0.1}"
FRONTEND_NEXT_PORT="${FRONTEND_NEXT_PORT:-4174}"

if [[ ! -x ".venv/bin/uvicorn" ]]; then
  echo "Missing .venv dependencies. Activate/setup .venv first."
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required for frontend-next but was not found on PATH."
  exit 1
fi

if [[ ! -d "frontend-next/node_modules" ]]; then
  echo "Missing frontend-next dependencies. Running npm install..."
  npm --prefix frontend-next install
fi

echo "Starting CloudTutor dev servers (backend + frontend-next)..."
echo "Backend:       http://${BACKEND_HOST}:${BACKEND_PORT}"
echo "Frontend-next: http://${FRONTEND_NEXT_HOST}:${FRONTEND_NEXT_PORT}"

# Avoid stale reload processes serving old code paths.
pkill -f "uvicorn backend.app.main:app" >/dev/null 2>&1 || true
pkill -f "next dev --turbopack --hostname ${FRONTEND_NEXT_HOST} --port ${FRONTEND_NEXT_PORT}" >/dev/null 2>&1 || true
rm -f frontend-next/.next/dev/lock >/dev/null 2>&1 || true

.venv/bin/uvicorn backend.app.main:app --reload --host "$BACKEND_HOST" --port "$BACKEND_PORT" &
BACKEND_PID=$!

NEXT_PUBLIC_BACKEND_URL="http://${BACKEND_HOST}:${BACKEND_PORT}" \
  npm --prefix frontend-next run dev -- --hostname "$FRONTEND_NEXT_HOST" --port "$FRONTEND_NEXT_PORT" &
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
