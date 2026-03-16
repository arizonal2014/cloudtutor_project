#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -d ".venv" ]]; then
  echo "Missing .venv in $ROOT_DIR"
  exit 1
fi

BACKEND_PORT="${BACKEND_ARTIFACT_TEST_PORT:-18087}"
BACKEND_LOG="/tmp/cloudtutor_session08_backend.log"

cleanup() {
  if [[ -n "${BACKEND_PID:-}" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "[1/4] Starting backend for Session 08 artifact verification..."
.venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" >"$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!

for _ in {1..30}; do
  if curl -sSf "http://127.0.0.1:${BACKEND_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.4
done

echo "[2/4] Generating tutorial artifact via API..."
.venv/bin/python - <<'PY' "$BACKEND_PORT"
from __future__ import annotations

import json
import sys
import urllib.request


def http_json(url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        status = response.getcode()
        body = response.read().decode("utf-8")
    return status, json.loads(body)


port = int(sys.argv[1])
base_url = f"http://127.0.0.1:{port}"
status, artifact = http_json(
    f"{base_url}/artifacts/tutorial",
    payload={
        "user_id": "session08-user",
        "session_id": "session08-test",
        "topic": "Hey, what's up",
        "user_transcript": "[10:00:01 PM] What is a Cloud Function HTTP trigger?\n[10:00:07 PM] show me more",
        "agent_transcript": (
            "[10:00:04 PM] An HTTP trigger lets you run a function via a web request URL.\n"
            "[10:00:12 PM] You can deploy a function, get an endpoint URL, and call it from apps or webhooks."
        ),
        "citations": [
            {
                "title": "Cloud Functions HTTP trigger",
                "url": "https://cloud.google.com/functions/docs/triggers/http",
            }
        ],
        "include_pdf": False,
    },
)

assert status == 200, status
assert isinstance(artifact.get("artifact_id"), str) and artifact["artifact_id"], artifact
assert isinstance(artifact.get("html_url"), str) and artifact["html_url"].startswith("/artifacts/"), artifact
assert isinstance(artifact.get("tutorial_steps"), list) and len(artifact["tutorial_steps"]) >= 3, artifact
assert isinstance(artifact.get("mermaid_diagram"), str) and "flowchart " in artifact["mermaid_diagram"], artifact
topic = str(artifact.get("topic", "")).lower()
assert "hey, what's up" not in topic, artifact
assert "function" in topic, artifact
summary = str(artifact.get("summary", "")).lower()
assert "show me more" not in summary, artifact
diagram = str(artifact.get("mermaid_diagram", "")).lower()
assert "apply this concept" not in diagram, artifact
assert "client" in diagram or "service" in diagram or "function" in diagram, artifact

print(json.dumps(artifact))
print("session08_artifact_create_ok")
PY

echo "[3/4] Verifying artifact HTML download endpoint..."
ARTIFACT_JSON="$(.venv/bin/python - <<'PY' "$BACKEND_PORT"
from __future__ import annotations

import json
import sys
import urllib.request

port = int(sys.argv[1])
base_url = f"http://127.0.0.1:{port}"

request = urllib.request.Request(
    f"{base_url}/artifacts/tutorial",
    data=json.dumps(
        {
            "user_id": "session08-user",
            "session_id": "session08-test-download",
            "topic": "Cloud Run basics",
            "user_transcript": "[10:10:00 PM] explain cloud run",
            "agent_transcript": "[10:10:04 PM] Cloud Run runs stateless containers and scales automatically.",
            "citations": [{"title": "Cloud Run docs", "url": "https://cloud.google.com/run/docs"}],
            "include_pdf": False,
        }
    ).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=30) as response:
    payload = json.loads(response.read().decode("utf-8"))
print(json.dumps(payload))
PY
)"

HTML_PATH="$(.venv/bin/python - <<'PY' "$ARTIFACT_JSON"
import json
import sys

payload = json.loads(sys.argv[1])
print(payload["html_url"])
PY
)"

HTML_CONTENT="$(curl -sSf "http://127.0.0.1:${BACKEND_PORT}${HTML_PATH}")"
echo "$HTML_CONTENT" | rg -q "CloudTutor Tutorial Artifact"
echo "$HTML_CONTENT" | rg -q "flowchart "

echo "[4/4] Session 08 artifact verification passed."
