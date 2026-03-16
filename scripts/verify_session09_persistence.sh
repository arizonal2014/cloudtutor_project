#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -d ".venv" ]]; then
  echo "Missing .venv in $ROOT_DIR"
  exit 1
fi

BACKEND_PORT="${BACKEND_PERSISTENCE_TEST_PORT:-18089}"
BACKEND_LOG="/tmp/cloudtutor_session09_backend.log"
SESSION_STORE_DIR="/tmp/cloudtutor_session09_store"
ARTIFACT_DIR="/tmp/cloudtutor_session09_artifacts"
TEST_USER_ID="session09-user"
TEST_SESSION_ID="session09-resume"

BACKEND_PID=""

start_backend() {
  CLOUDTUTOR_SESSION_STORE_DIR="$SESSION_STORE_DIR" \
    CLOUDTUTOR_ARTIFACT_DIR="$ARTIFACT_DIR" \
    CLOUDTUTOR_FIRESTORE_ENABLED=0 \
    .venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" >"$BACKEND_LOG" 2>&1 &
  BACKEND_PID=$!

  for _ in {1..40}; do
    if curl -sSf "http://127.0.0.1:${BACKEND_PORT}/health" >/dev/null 2>&1; then
      return
    fi
    sleep 0.3
  done

  echo "Backend failed to start. Log:"
  cat "$BACKEND_LOG" || true
  exit 1
}

stop_backend() {
  if [[ -n "$BACKEND_PID" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
  BACKEND_PID=""
}

cleanup() {
  stop_backend
}
trap cleanup EXIT INT TERM

rm -rf "$SESSION_STORE_DIR" "$ARTIFACT_DIR"
mkdir -p "$SESSION_STORE_DIR" "$ARTIFACT_DIR"

echo "[1/6] Starting backend for Session 09 verification..."
start_backend

echo "[2/6] Creating a resumable session snapshot via persistence API..."
.venv/bin/python - <<'PY' "$BACKEND_PORT" "$TEST_USER_ID" "$TEST_SESSION_ID"
from __future__ import annotations

import json
import sys
import urllib.request

port = int(sys.argv[1])
user_id = sys.argv[2]
session_id = sys.argv[3]
base = f"http://127.0.0.1:{port}"

request = urllib.request.Request(
    f"{base}/sessions/{user_id}/{session_id}/events",
    data=json.dumps(
        {
            "role": "user",
            "event_type": "seed_user_turn",
            "text": "show me more about cloud run",
            "dialogue_state": {
                "current_topic": "show me more about cloud run",
                "awaiting_doc_confirmation": True,
                "branch_context": "awaiting_doc_confirmation",
                "last_intent": "request_more_details",
            },
            "metadata": {"source": "session09_verifier"},
        }
    ).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=30) as response:
    snapshot = json.loads(response.read().decode("utf-8"))

assert snapshot.get("event_count", 0) >= 1, snapshot
print("session09_seed_ok")
PY

echo "[3/6] Validating session snapshot + artifact creation..."
ARTIFACT_JSON="$(
  .venv/bin/python - <<'PY' "$BACKEND_PORT" "$TEST_USER_ID" "$TEST_SESSION_ID"
from __future__ import annotations

import json
import sys
import urllib.request

port = int(sys.argv[1])
user_id = sys.argv[2]
session_id = sys.argv[3]
base = f"http://127.0.0.1:{port}"

with urllib.request.urlopen(f"{base}/sessions/{user_id}/{session_id}", timeout=30) as response:
    snapshot = json.loads(response.read().decode("utf-8"))

assert snapshot["event_count"] >= 1, snapshot
assert "cloud run" in snapshot["dialogue_state"].get("current_topic", "").lower(), snapshot
assert "show me more about cloud run" in snapshot.get("user_transcript", "").lower(), snapshot

request = urllib.request.Request(
    f"{base}/artifacts/tutorial",
    data=json.dumps(
        {
            "user_id": user_id,
            "session_id": session_id,
            "topic": "Cloud Run intro",
            "user_transcript": snapshot.get("user_transcript", ""),
            "agent_transcript": "[10:02:00 PM] Cloud Run deploys stateless containers and scales automatically.",
            "citations": [
                {
                    "title": "Cloud Run docs",
                    "url": "https://cloud.google.com/run/docs",
                }
            ],
            "include_pdf": False,
        }
    ).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=30) as response:
    artifact = json.loads(response.read().decode("utf-8"))

assert artifact.get("artifact_id"), artifact
assert str(artifact.get("html_url", "")).startswith("/artifacts/"), artifact
print(json.dumps(artifact))
PY
)"

ARTIFACT_ID="$(
  .venv/bin/python - <<'PY' "$ARTIFACT_JSON"
import json
import sys

payload = json.loads(sys.argv[1])
print(payload["artifact_id"])
PY
)"

ARTIFACT_HTML_URL="$(
  .venv/bin/python - <<'PY' "$ARTIFACT_JSON"
import json
import sys

payload = json.loads(sys.argv[1])
print(payload["html_url"])
PY
)"

echo "[4/6] Restarting backend and validating persisted reload..."
stop_backend
start_backend

.venv/bin/python - <<'PY' "$BACKEND_PORT" "$TEST_USER_ID" "$TEST_SESSION_ID" "$ARTIFACT_ID"
from __future__ import annotations

import json
import sys
import urllib.request

port = int(sys.argv[1])
user_id = sys.argv[2]
session_id = sys.argv[3]
artifact_id = sys.argv[4]
base = f"http://127.0.0.1:{port}"

with urllib.request.urlopen(f"{base}/sessions/{user_id}/{session_id}", timeout=30) as response:
    snapshot = json.loads(response.read().decode("utf-8"))

assert snapshot["event_count"] >= 1, snapshot
assert "cloud run" in snapshot["dialogue_state"].get("current_topic", "").lower(), snapshot

with urllib.request.urlopen(f"{base}/artifacts/recent?limit=10", timeout=30) as response:
    recent = json.loads(response.read().decode("utf-8"))

assert isinstance(recent, list), recent
assert any(item.get("artifact_id") == artifact_id for item in recent), recent

print("session09_restart_reload_ok")
PY

echo "[5/6] Verifying artifact HTML still serves after restart..."
HTML_CONTENT="$(curl -sSf "http://127.0.0.1:${BACKEND_PORT}${ARTIFACT_HTML_URL}")"
echo "$HTML_CONTENT" | rg -q "CloudTutor Tutorial Artifact"
echo "$HTML_CONTENT" | rg -q "Cloud Run"

echo "[6/6] Session 09 persistence verification passed."
