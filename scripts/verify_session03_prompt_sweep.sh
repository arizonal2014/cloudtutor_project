#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -d ".venv" ]]; then
  echo "Missing .venv in $ROOT_DIR"
  exit 1
fi

BACKEND_PORT="${BACKEND_FLOW_SWEEP_TEST_PORT:-18085}"
BACKEND_LOG="/tmp/cloudtutor_session03_sweep_backend.log"

cleanup() {
  if [[ -n "${BACKEND_PID:-}" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "[1/5] Starting backend for Session 03 prompt sweep..."
.venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" >"$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!

for _ in {1..30}; do
  if curl -sSf "http://127.0.0.1:${BACKEND_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.4
done

echo "[2/5] Checking credential readiness..."
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
  echo "No usable credentials detected. Skipping Session 03 10-prompt sweep."
  exit 0
fi

echo "[3/5] Running 10-prompt behavior sweep..."
.venv/bin/python - <<'PY' "$BACKEND_PORT"
import asyncio
import json
import os
import sys
import websockets

LOCAL_REASONS = {
    "doc_confirmation_required",
    "confirmation_unclear",
    "ambiguous_fallback",
}

PROMPT_STEPS = [
    {"prompt": "What is Cloud Run in one short sentence?", "expect": "model"},
    {"prompt": "show me more about cloud run", "expect": "reason", "reason": "doc_confirmation_required"},
    {"prompt": "maybe", "expect": "reason", "reason": "confirmation_unclear"},
    {"prompt": "no", "expect": "model"},
    {"prompt": "maybe", "expect": "reason", "reason": "ambiguous_fallback"},
    {"prompt": "tell me more about IAM roles", "expect": "reason", "reason": "doc_confirmation_required"},
    {"prompt": "yes", "expect": "model"},
    {"prompt": "maybe", "expect": "reason", "reason": "ambiguous_fallback"},
    {"prompt": "How do IAM roles differ from permissions?", "expect": "model"},
    {"prompt": "Compare GKE and Cloud Run briefly.", "expect": "model"},
]


async def recv_frame(ws, timeout: float = 0.75):
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return "bytes", bytes(raw)
    return "json", json.loads(raw)


def _extract_text(msg: dict) -> str:
    output_text = ((msg.get("output_transcription") or {}).get("text") or "").strip()
    if output_text:
        return output_text
    for part in msg.get("parts", []):
        if part.get("type") == "text":
            data = part.get("data")
            if isinstance(data, str) and data.strip():
                return data.strip()
    return ""


async def wait_for_reason(ws, expected_reason: str, timeout_sec: float = 20.0) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_sec
    while loop.time() < deadline:
        try:
            kind, payload = await recv_frame(ws)
        except TimeoutError:
            continue

        if kind == "bytes":
            continue

        msg = payload
        if msg.get("type") == "error":
            raise RuntimeError(f"Server error event: {msg}")
        if msg.get("type") != "agent_event":
            continue

        if msg.get("reason") != expected_reason:
            continue

        text = _extract_text(msg).lower()
        if expected_reason in {"doc_confirmation_required", "confirmation_unclear"}:
            assert "yes" in text and "no" in text, msg
        return msg.get("reason")

    raise AssertionError(f"Timed out waiting for reason={expected_reason}")


async def wait_for_model_reply(ws, timeout_sec: float = 75.0) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_sec
    saw_payload = False
    summary = ""

    while loop.time() < deadline:
        remaining = max(0.0, deadline - loop.time())
        try:
            kind, payload = await recv_frame(ws, timeout=min(1.0, remaining))
        except TimeoutError:
            if saw_payload:
                return summary or "model_payload"
            continue

        if kind == "bytes":
            saw_payload = True
            summary = f"binary_audio_bytes={len(payload)}"
            continue

        msg = payload
        if msg.get("type") == "error":
            raise RuntimeError(f"Server error event: {msg}")
        if msg.get("type") != "agent_event":
            continue

        reason = msg.get("reason")
        if reason in LOCAL_REASONS:
            raise AssertionError(f"Unexpected local reason while awaiting model response: {msg}")

        output_text = _extract_text(msg)
        if output_text:
            saw_payload = True
            summary = output_text[:140]
        else:
            for part in msg.get("parts", []):
                if part.get("type") == "audio/pcm":
                    saw_payload = True
                    summary = part.get("mime_type", "audio/pcm")
                    break

        if msg.get("turn_complete") and saw_payload:
            return summary or "turn_complete"

    if saw_payload:
        return summary or "model_payload"
    raise AssertionError("Timed out waiting for model response payload")


async def run_sweep(port: int) -> None:
    session_suffix = os.urandom(4).hex()
    uri = f"ws://127.0.0.1:{port}/ws/flow-sweep-user/session03-prompts-{session_suffix}"
    results: list[str] = []

    async with websockets.connect(uri, max_size=None) as ws:
        connection_event = json.loads(await ws.recv())
        assert connection_event.get("type") == "connection", connection_event

        for idx, step in enumerate(PROMPT_STEPS, start=1):
            prompt = step["prompt"]
            await ws.send(json.dumps({"mime_type": "text/plain", "data": prompt}))

            if step["expect"] == "reason":
                reason = await wait_for_reason(ws, step["reason"])
                results.append(f"{idx:02d}. reason={reason} prompt={prompt}")
            else:
                summary = await wait_for_model_reply(ws)
                results.append(f"{idx:02d}. model_reply={summary} prompt={prompt}")

    for line in results:
        print(line)
    print("session03_prompt_sweep_ok")


asyncio.run(run_sweep(int(sys.argv[1])))
PY

echo "[4/5] Sweep passed."
echo "[5/5] Session 03 prompt sweep verification passed."
