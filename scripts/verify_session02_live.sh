#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -d ".venv" ]]; then
  echo "Missing .venv in $ROOT_DIR"
  exit 1
fi

BACKEND_PORT="${BACKEND_LIVE_TEST_PORT:-18082}"
BACKEND_LOG="/tmp/cloudtutor_session02_backend.log"

cleanup() {
  if [[ -n "${BACKEND_PID:-}" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM


echo "[1/4] Starting backend for Session 02 live verification..."
.venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" >"$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!

for _ in {1..30}; do
  if curl -sSf "http://127.0.0.1:${BACKEND_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.4
done


echo "[2/4] Checking credential readiness..."
CREDENTIAL_READY="$(.venv/bin/python - <<'PY'
import os
from pathlib import Path
from dotenv import dotenv_values

def is_placeholder(value: str | None) -> bool:
    if not value:
        return True
    upper = value.upper()
    markers = ["REPLACE", "PASTE", "YOUR_", "<", ">"]
    return any(marker in upper for marker in markers)

root = Path.cwd()
merged = {}
for path in [root / '.env', root / 'cloud_tutor_agent' / '.env']:
    if path.exists():
        merged.update({k: v for k, v in dotenv_values(path).items() if v is not None})

for key in ["GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_API_KEY", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"]:
    if os.getenv(key):
        merged[key] = os.getenv(key)

use_vertex = str(merged.get("GOOGLE_GENAI_USE_VERTEXAI", "0")).strip().lower() in {"1", "true", "yes"}

if use_vertex:
    project = merged.get("GOOGLE_CLOUD_PROJECT")
    location = merged.get("GOOGLE_CLOUD_LOCATION")
    ready = (not is_placeholder(project)) and (not is_placeholder(location))
else:
    api_key = merged.get("GOOGLE_API_KEY")
    ready = not is_placeholder(api_key)

print("1" if ready else "0")
PY
)"

if [[ "$CREDENTIAL_READY" != "1" ]]; then
  echo "No usable credentials detected. Skipping live model roundtrip verification."
  echo "Set cloud_tutor_agent/.env with valid API key or Vertex config, then rerun."
  exit 0
fi


echo "[3/4] Verifying websocket ping + live text roundtrip..."
.venv/bin/python - <<'PY' "$BACKEND_PORT"
import asyncio
import json
import sys
import websockets

port = int(sys.argv[1])

async def main() -> None:
    uri = f"ws://127.0.0.1:{port}/ws/live-user/live-session"
    async with websockets.connect(uri, max_size=None) as ws:
        connection_event = None
        for _ in range(5):
            evt = json.loads(await ws.recv())
            if evt.get("type") == "connection":
                connection_event = evt
                break
        assert connection_event is not None and connection_event.get("type") == "connection", "Did not receive connection event"

        await ws.send(json.dumps({"type": "ping"}))
        pong = None
        for _ in range(5):
            evt = json.loads(await ws.recv())
            if evt.get("type") == "session_state":
                continue
            if evt.get("type") == "pong":
                pong = evt
                break
        assert pong is not None and pong.get("type") == "pong", pong

        await ws.send(json.dumps({"mime_type": "text/plain", "data": "Say hello in one short sentence."}))

        got_live_response = False
        for _ in range(480):  # up to ~120s with timeout below
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.25)
            except TimeoutError:
                continue

            if isinstance(raw, (bytes, bytearray, memoryview)):
                got_live_response = True
                break

            msg = json.loads(raw)
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "error":
                raise RuntimeError(f"Server returned error event: {msg}")

            if msg.get("type") == "agent_event":
                output_transcription = msg.get("output_transcription") or {}
                if output_transcription.get("text"):
                    got_live_response = True
                    break
                for part in msg.get("parts", []):
                    if part.get("type") in {"text", "audio/pcm"}:
                        got_live_response = True
                        break
                if got_live_response:
                    break

        assert got_live_response, "No live response event received"

    print("session02_live_roundtrip_ok")

asyncio.run(main())
PY


echo "[4/4] Session 02 live verification passed."
