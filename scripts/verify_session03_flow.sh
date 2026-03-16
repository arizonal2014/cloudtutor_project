#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -d ".venv" ]]; then
  echo "Missing .venv in $ROOT_DIR"
  exit 1
fi

BACKEND_PORT="${BACKEND_FLOW_TEST_PORT:-18083}"
BACKEND_LOG="/tmp/cloudtutor_session03_backend.log"

cleanup() {
  if [[ -n "${BACKEND_PID:-}" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "[1/4] Starting backend for Session 03 flow verification..."
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
  echo "No usable credentials detected. Skipping Session 03 flow verification."
  exit 0
fi

echo "[3/4] Verifying confirmation gate for 'show me more' intent..."
.venv/bin/python - <<'PY' "$BACKEND_PORT"
import asyncio
import json
import os
import sys
import websockets

port = int(sys.argv[1])


async def recv_until(ws, matcher, limit=120):
    for _ in range(limit):
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
        except TimeoutError:
            continue
        if isinstance(raw, (bytes, bytearray, memoryview)):
            continue
        msg = json.loads(raw)
        if matcher(msg):
            return msg
    raise AssertionError("Expected event not received")


async def main() -> None:
    session_suffix = os.urandom(4).hex()
    uri = f"ws://127.0.0.1:{port}/ws/flow-user/flow-session-{session_suffix}"
    async with websockets.connect(uri, max_size=None) as ws:
        connection_event = json.loads(await ws.recv())
        assert connection_event["type"] == "connection", connection_event

        await ws.send(json.dumps({"mime_type": "text/plain", "data": "show me more about cloud run"}))

        first_gate = await recv_until(
            ws,
            lambda m: m.get("type") == "agent_event"
            and m.get("reason") == "doc_confirmation_required",
        )
        text = ((first_gate.get("output_transcription") or {}).get("text") or "").lower()
        assert "yes" in text and "no" in text, first_gate

        await ws.send(json.dumps({"mime_type": "text/plain", "data": "maybe"}))

        unclear = await recv_until(
            ws,
            lambda m: m.get("type") == "agent_event"
            and m.get("reason") == "confirmation_unclear",
        )
        unclear_text = ((unclear.get("output_transcription") or {}).get("text") or "").lower()
        assert "yes" in unclear_text and "no" in unclear_text, unclear

    print("session03_flow_gate_ok")


asyncio.run(main())
PY

echo "[4/4] Session 03 flow verification passed."
