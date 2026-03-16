#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -d ".venv" ]]; then
  echo "Missing .venv in $ROOT_DIR"
  exit 1
fi

BACKEND_PORT="${BACKEND_HARDENING_TEST_PORT:-18089}"
BACKEND_LOG="/tmp/cloudtutor_session11_backend.log"

cleanup() {
  if [[ -n "${BACKEND_PID:-}" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "[1/5] Verifying threaded computer backend isolation..."
.venv/bin/python - <<'PY'
from __future__ import annotations

from backend.app.computer_use.computer import EnvState
from backend.app.computer_use.threaded_backend import ThreadedComputerBackend


class FakeComputer:
    def __init__(self) -> None:
        self.debug_url = "about:blank"
        self._size = (1280, 720)

    def __enter__(self) -> "FakeComputer":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def screen_size(self):
        return self._size

    def open_web_browser(self):
        return EnvState(screenshot=b"fake", url=self.debug_url)

    def click_at(self, x, y):
        self.debug_url = f"https://example.test/click/{x}/{y}"
        return EnvState(screenshot=b"fake", url=self.debug_url)

    def hover_at(self, x, y):
        return EnvState(screenshot=b"fake", url=self.debug_url)

    def type_text_at(self, x, y, text, press_enter, clear_before_typing):
        return EnvState(screenshot=b"fake", url=self.debug_url)

    def scroll_document(self, direction):
        return EnvState(screenshot=b"fake", url=self.debug_url)

    def scroll_at(self, x, y, direction, magnitude):
        return EnvState(screenshot=b"fake", url=self.debug_url)

    def wait_5_seconds(self):
        return EnvState(screenshot=b"fake", url=self.debug_url)

    def go_back(self):
        return EnvState(screenshot=b"fake", url=self.debug_url)

    def go_forward(self):
        return EnvState(screenshot=b"fake", url=self.debug_url)

    def search(self):
        return EnvState(screenshot=b"fake", url=self.debug_url)

    def navigate(self, url: str):
        self.debug_url = url
        return EnvState(screenshot=b"fake", url=self.debug_url)

    def key_combination(self, keys):
        return EnvState(screenshot=b"fake", url=self.debug_url)

    def drag_and_drop(self, x, y, destination_x, destination_y):
        return EnvState(screenshot=b"fake", url=self.debug_url)

    def current_state(self):
        return EnvState(screenshot=b"fake", url=self.debug_url)


backend = ThreadedComputerBackend(backend_factory=FakeComputer)
backend.__enter__()
assert backend.screen_size() == (1280, 720)
result = backend.click_at(10, 20)
assert result.url.endswith("/10/20"), result
assert backend.debug_url and backend.debug_url.endswith("/10/20")
backend.__exit__(None, None, None)
print("threaded_backend_ok")
PY

echo "[2/5] Starting backend for Session 11 hardening verification..."
.venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" >"$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!

for _ in {1..35}; do
  if curl -sSf "http://127.0.0.1:${BACKEND_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.4
done

echo "[3/5] Verifying request-id middleware and health payload..."
HEADER_CAPTURE="/tmp/cloudtutor_session11_headers.txt"
BODY_CAPTURE="/tmp/cloudtutor_session11_body.json"
curl -sS -D "$HEADER_CAPTURE" "http://127.0.0.1:${BACKEND_PORT}/health" -o "$BODY_CAPTURE" >/dev/null
rg -q "^x-request-id:" "$HEADER_CAPTURE"
.venv/bin/python - <<'PY' "$BODY_CAPTURE"
from __future__ import annotations
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload.get("status") == "ok", payload
status = payload.get("firestore_status")
assert isinstance(status, dict), payload
assert "enabled" in status and "failure_limit" in status, payload
print("health_payload_ok")
PY

echo "[4/5] Verifying websocket ping path remains stable..."
.venv/bin/python - <<'PY' "$BACKEND_PORT"
from __future__ import annotations

import asyncio
import json
import sys

import websockets


async def main(port: int) -> None:
    uri = f"ws://127.0.0.1:{port}/ws/session11-user/session11-test"
    async with websockets.connect(uri, max_size=None) as ws:
        first = json.loads(await ws.recv())
        assert first.get("type") == "connection", first
        await ws.send(json.dumps({"type": "ping"}))
        deadline = asyncio.get_running_loop().time() + 5.0
        while True:
            frame = json.loads(await ws.recv())
            if frame.get("type") == "pong":
                return
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"Timed out waiting for pong. Last frame: {frame}")


asyncio.run(main(int(sys.argv[1])))
print("websocket_ping_ok")
PY

echo "[5/5] Session 11 hardening verification passed."
