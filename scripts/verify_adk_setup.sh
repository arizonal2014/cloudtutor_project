#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -d ".venv" ]]; then
  echo "Missing .venv in $ROOT_DIR"
  exit 1
fi

source .venv/bin/activate

echo "[1/4] Checking ADK CLI..."
adk --version

echo "[2/4] Importing agent module..."
python - <<'PY'
from cloud_tutor_agent.agent import root_agent
print(f"agent={root_agent.name} model={root_agent.model}")
PY

echo "[3/4] Starting ADK web server and checking HTTP response..."
PORT="${ADK_VERIFY_PORT:-9010}"
LOG_FILE="/tmp/cloud_tutor_adk_web.log"
HTML_FILE="/tmp/cloud_tutor_adk_web.html"
rm -f "$LOG_FILE" "$HTML_FILE"

adk web --host 127.0.0.1 --port "$PORT" >"$LOG_FILE" 2>&1 &
ADK_PID=$!
cleanup() {
  if kill -0 "$ADK_PID" 2>/dev/null; then
    kill "$ADK_PID" 2>/dev/null || true
    wait "$ADK_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

sleep 8
curl -sSLf "http://127.0.0.1:${PORT}/" >"$HTML_FILE"
BYTES="$(wc -c < "$HTML_FILE" | tr -d '[:space:]')"
if [[ "$BYTES" -le 0 ]]; then
  echo "ADK web response is empty"
  exit 1
fi
echo "adk_web_response_bytes=$BYTES"
cleanup
trap - EXIT

echo "[4/4] Sanity note..."
echo "Setup is valid. To run model calls, set a real API key in cloud_tutor_agent/.env."
