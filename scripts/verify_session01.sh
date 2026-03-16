#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -d ".venv" ]]; then
  echo "Missing .venv in $ROOT_DIR"
  exit 1
fi

BACKEND_PORT="${BACKEND_TEST_PORT:-18080}"
FRONTEND_PORT="${FRONTEND_TEST_PORT:-14173}"
BACKEND_LOG="/tmp/cloudtutor_session01_backend.log"
FRONTEND_LOG="/tmp/cloudtutor_session01_frontend.log"

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


echo "[1/5] Starting backend for smoke test..."
.venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" >"$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!

for _ in {1..25}; do
  if curl -sSf "http://127.0.0.1:${BACKEND_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.4
done

HEALTH_JSON="$(curl -sSf "http://127.0.0.1:${BACKEND_PORT}/health")"

echo "[2/5] Verifying /health payload..."
.venv/bin/python - <<'PY' "$HEALTH_JSON"
import json
import sys
payload = json.loads(sys.argv[1])
assert payload.get("status") == "ok", payload
assert payload.get("service") == "cloudtutor-backend", payload
print("health_ok")
PY


echo "[3/5] Verifying websocket handshake + ping path..."
.venv/bin/python - <<'PY' "$BACKEND_PORT"
import asyncio
import json
import sys
import websockets

port = int(sys.argv[1])

async def main() -> None:
    uri = f"ws://127.0.0.1:{port}/ws/test-user/test-session"
    async with websockets.connect(uri) as ws:
        connection_event = json.loads(await ws.recv())
        assert connection_event["type"] == "connection", connection_event

        await ws.send(json.dumps({"type": "ping"}))
        pong_event = json.loads(await ws.recv())
        assert pong_event["type"] == "pong", pong_event

    print("websocket_ping_ok")

asyncio.run(main())
PY


echo "[4/5] Starting frontend static server..."
.venv/bin/python -m http.server "$FRONTEND_PORT" --bind 127.0.0.1 --directory frontend >"$FRONTEND_LOG" 2>&1 &
FRONTEND_PID=$!

for _ in {1..25}; do
  if curl -sSf "http://127.0.0.1:${FRONTEND_PORT}/index.html" >/dev/null 2>&1; then
    break
  fi
  sleep 0.3
done

curl -sSf "http://127.0.0.1:${FRONTEND_PORT}/index.html" | rg -q "CloudTutor Session 02 Frontend"
echo "[5/5] Frontend load check passed."

echo "Session 01 smoke test passed."
