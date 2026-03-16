#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -d ".venv" ]]; then
  echo "Missing .venv in $ROOT_DIR"
  exit 1
fi

BACKEND_PORT="${BACKEND_GROUNDING_TEST_PORT:-18086}"
BACKEND_LOG="/tmp/cloudtutor_session04_backend.log"

cleanup() {
  if [[ -n "${BACKEND_PID:-}" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "[1/5] Starting backend for Session 04 grounding verification..."
.venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" >"$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!

for _ in {1..40}; do
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
  echo "No usable credentials detected. Skipping Session 04 grounding verification."
  exit 0
fi

echo "[3/5] Running 10-question grounding benchmark..."
.venv/bin/python - <<'PY' "$BACKEND_PORT"
import asyncio
import json
import re
import sys
import websockets

URL_PATTERN = re.compile(r"https?://[^\s<>()\"']+")

PROMPTS = [
    "In 2 short sentences, what is the latest Cloud Run pricing model? Include source links.",
    "In 2 short sentences, what are the current Cloud Run free tier limits? Include sources.",
    "In 2 short sentences, what is the current default timeout limit for Cloud Run services? Include sources.",
    "In 2 short sentences, what are the latest Firebase Hosting pricing tiers? Include sources.",
    "In 2 short sentences, what is the latest announced stable Kubernetes version in GKE channels? Include sources.",
    "In 2 short sentences, what is the current BigQuery on-demand query pricing per TiB? Include sources.",
    "In 2 short sentences, what are the current Firestore write and read pricing basics? Include sources.",
    "In 2 short sentences, what is the current Cloud Storage standard class pricing model summary? Include sources.",
    "In 2 short sentences, what are the current Cloud Build default free quota details? Include sources.",
    "In 2 short sentences, what are the current Vertex AI text generation pricing considerations? Include sources.",
]


def normalize_url(url: str) -> str:
    value = url.strip()
    while value and value[-1] in ".,);]}>\"'":
        value = value[:-1]
    return value


def extract_urls_from_text(text: str) -> set[str]:
    return {normalize_url(found) for found in URL_PATTERN.findall(text)}


def extract_citation_urls(message: dict) -> set[str]:
    urls: set[str] = set()
    for citation in message.get("citations") or []:
        if isinstance(citation, dict):
            url = citation.get("url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                urls.add(normalize_url(url))
    return urls


async def collect_turn(ws, timeout_sec: float = 70.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_sec
    last_activity = loop.time()

    saw_output = False
    text_parts: list[str] = []
    urls: set[str] = set()
    saw_tool_event = False

    while loop.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
        except TimeoutError:
            if saw_output and (loop.time() - last_activity) > 4.0:
                break
            continue

        last_activity = loop.time()

        if isinstance(raw, (bytes, bytearray, memoryview)):
            continue

        msg = json.loads(raw)
        if msg.get("type") == "error":
            raise RuntimeError(f"Server error event: {msg}")
        if msg.get("type") != "agent_event":
            continue

        urls.update(extract_citation_urls(msg))

        output = (msg.get("output_transcription") or {}).get("text")
        if isinstance(output, str) and output.strip():
            text_parts.append(output.strip())
            urls.update(extract_urls_from_text(output))
            saw_output = True

        for part in msg.get("parts", []):
            part_type = part.get("type")
            if part_type == "function_response":
                saw_tool_event = True
                response = (part.get("data") or {}).get("response")
                urls.update(extract_urls_from_text(json.dumps(response, default=str)))
            if part_type == "text":
                data = part.get("data")
                if isinstance(data, str) and data.strip():
                    text_parts.append(data.strip())
                    urls.update(extract_urls_from_text(data))
                    saw_output = True

        if msg.get("turn_complete"):
            break

    text = "\n".join(text_parts).strip()
    return text, sorted(urls), saw_tool_event


async def main(port: int) -> None:
    uri = f"ws://127.0.0.1:{port}/ws/ground-user/ground-session"
    pass_count = 0
    results: list[dict] = []

    async with websockets.connect(uri, max_size=None) as ws:
        connection_event = json.loads(await ws.recv())
        assert connection_event.get("type") == "connection", connection_event

        for idx, prompt in enumerate(PROMPTS, start=1):
            await ws.send(json.dumps({"mime_type": "text/plain", "data": prompt}))
            text, urls, saw_tool_event = await collect_turn(ws)
            grounded = len(urls) > 0
            if grounded:
                pass_count += 1

            results.append(
                {
                    "index": idx,
                    "grounded": grounded,
                    "tool_event": saw_tool_event,
                    "url_count": len(urls),
                    "sample_urls": urls[:3],
                    "preview": text[:180],
                }
            )

    for row in results:
        status = "PASS" if row["grounded"] else "FAIL"
        print(
            f"{row['index']:02d}. {status} "
            f"url_count={row['url_count']} tool_event={row['tool_event']} "
            f"preview={row['preview']!r} sample_urls={row['sample_urls']}"
        )

    print(f"session04_grounding_score={pass_count}/10")
    assert pass_count >= 8, f"Grounding score below threshold: {pass_count}/10"
    print("session04_grounding_ok")


asyncio.run(main(int(sys.argv[1])))
PY

echo "[4/5] Grounding benchmark passed."
echo "[5/5] Session 04 grounding verification passed."
