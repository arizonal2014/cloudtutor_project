#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -d ".venv" ]]; then
  echo "Missing .venv in $ROOT_DIR"
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required for frontend-next verification but was not found on PATH."
  exit 1
fi

BACKEND_PORT="${BACKEND_NEXT_TEST_PORT:-18084}"
FRONTEND_PORT="${FRONTEND_NEXT_TEST_PORT:-14174}"
BACKEND_LOG="/tmp/cloudtutor_frontend_next_backend.log"
FRONTEND_LOG="/tmp/cloudtutor_frontend_next.log"

cleanup() {
  if [[ -n "${BACKEND_PID:-}" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
  if [[ -n "${FRONTEND_PID:-}" ]] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
    kill "$FRONTEND_PID" 2>/dev/null || true
    wait "$FRONTEND_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if [[ ! -d "frontend-next/node_modules" ]]; then
  echo "[setup] Installing frontend-next dependencies..."
  npm --prefix frontend-next install
fi

echo "[1/5] Starting backend..."
.venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" >"$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!

for _ in {1..30}; do
  if curl -sSf "http://127.0.0.1:${BACKEND_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.4
done


echo "[2/5] Starting frontend-next dev server..."
NEXT_PUBLIC_BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}" \
  npm --prefix frontend-next run dev -- --hostname 127.0.0.1 --port "$FRONTEND_PORT" >"$FRONTEND_LOG" 2>&1 &
FRONTEND_PID=$!

for _ in {1..60}; do
  if curl -sSf "http://127.0.0.1:${FRONTEND_PORT}" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done


echo "[3/5] Verifying page title and heading..."
PAGE_HTML="$(curl -sSf "http://127.0.0.1:${FRONTEND_PORT}")"

echo "$PAGE_HTML" | rg -q "CloudTutor Realtime Frontend \(Next.js\)"
echo "$PAGE_HTML" | rg -q "Live Documentation Navigator"
if echo "$PAGE_HTML" | rg -q "Run Computer Use"; then
  echo "Unexpected manual Computer Use run control found in UI."
  exit 1
fi

echo "[4/5] Verifying worklet asset..."
WORKLET_JS="$(curl -sSf "http://127.0.0.1:${FRONTEND_PORT}/audio-capture-worklet.js")"
echo "$WORKLET_JS" | rg -q "registerProcessor"


echo "[5/5] Frontend-next smoke passed."
