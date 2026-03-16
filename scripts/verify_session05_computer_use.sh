#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Missing .venv Python. Set up environment first."
  exit 1
fi

echo "[1/4] Running deterministic Computer Use worker checks..."

.venv/bin/python - <<'PY'
from __future__ import annotations

from types import SimpleNamespace

from google.genai import types

from backend.app.computer_use.computer import Computer, EnvState
from backend.app.computer_use.worker import ComputerUseWorker


class FakeComputer(Computer):
    def __init__(self) -> None:
        self._url = "https://start.local"
        self.clicked_positions: list[tuple[int, int]] = []
        self.navigation_history: list[str] = []

    def screen_size(self) -> tuple[int, int]:
        return (1000, 1000)

    def _state(self) -> EnvState:
        return EnvState(screenshot=b"fake-image", url=self._url)

    def open_web_browser(self) -> EnvState:
        return self._state()

    def click_at(self, x: int, y: int) -> EnvState:
        self.clicked_positions.append((x, y))
        return self._state()

    def hover_at(self, x: int, y: int) -> EnvState:
        return self._state()

    def type_text_at(
        self,
        x: int,
        y: int,
        text: str,
        press_enter: bool,
        clear_before_typing: bool,
    ) -> EnvState:
        return self._state()

    def scroll_document(self, direction):
        return self._state()

    def scroll_at(self, x: int, y: int, direction, magnitude: int):
        return self._state()

    def wait_5_seconds(self) -> EnvState:
        return self._state()

    def go_back(self) -> EnvState:
        return self._state()

    def go_forward(self) -> EnvState:
        return self._state()

    def search(self) -> EnvState:
        self._url = "https://www.google.com"
        return self._state()

    def navigate(self, url: str) -> EnvState:
        normalized = url if url.startswith("http") else f"https://{url}"
        self._url = normalized
        self.navigation_history.append(normalized)
        return self._state()

    def key_combination(self, keys: list[str]) -> EnvState:
        return self._state()

    def drag_and_drop(self, x: int, y: int, destination_x: int, destination_y: int) -> EnvState:
        return self._state()

    def current_state(self) -> EnvState:
        return self._state()


class FakeModelClient:
    def __init__(self, responses):
        self._responses = responses
        self._cursor = 0

    def generate_content(self, *, model, contents, config):
        if self._cursor >= len(self._responses):
            raise RuntimeError("Fake response sequence exhausted")
        response = self._responses[self._cursor]
        self._cursor += 1
        return response


def make_response(*, text: str | None = None, function_calls: list[types.FunctionCall] | None = None):
    parts = []
    if text:
        parts.append(types.Part(text=text))
    for function_call in function_calls or []:
        parts.append(types.Part(function_call=function_call))
    candidate = SimpleNamespace(content=types.Content(role="model", parts=parts))
    return SimpleNamespace(candidates=[candidate])


# Case 1: deterministic multi-step completion.
computer = FakeComputer()
responses = [
    make_response(
        function_calls=[types.FunctionCall(name="navigate", args={"url": "example.com"})]
    ),
    make_response(
        function_calls=[types.FunctionCall(name="click_at", args={"x": 500, "y": 500})]
    ),
    make_response(text="Task complete"),
]
worker = ComputerUseWorker(computer=computer, model_client=FakeModelClient(responses))
result = worker.run(query="go to example and click center", max_steps=10)

assert result.status == "completed", result.status
assert result.completed_steps == 2, result.completed_steps
assert computer.navigation_history == ["https://example.com"], computer.navigation_history
assert computer.clicked_positions == [(500, 500)], computer.clicked_positions


# Case 2: unsupported action is handled, not crashed.
computer = FakeComputer()
responses = [
    make_response(
        function_calls=[types.FunctionCall(name="open_spreadsheet", args={"sheet": "Q1"})]
    ),
    make_response(text="done"),
]
worker = ComputerUseWorker(computer=computer, model_client=FakeModelClient(responses))
result = worker.run(query="test unsupported action handling", max_steps=5)
assert result.status == "completed", result.status
assert any(step.status == "unsupported" for step in result.steps), result.steps


# Case 3: safety decision requires explicit confirmation and is blocked.
computer = FakeComputer()
responses = [
    make_response(
        function_calls=[
            types.FunctionCall(
                name="click_at",
                args={
                    "x": 10,
                    "y": 20,
                    "safety_decision": {
                        "decision": "require_confirmation",
                        "explanation": "Sensitive action",
                    },
                },
            )
        ]
    ),
]
worker = ComputerUseWorker(computer=computer, model_client=FakeModelClient(responses))
result = worker.run(query="do sensitive action", max_steps=3)
assert result.status == "awaiting_confirmation", result.status
assert result.steps[0].status == "blocked_by_safety", result.steps
assert computer.clicked_positions == [], computer.clicked_positions
assert result.pending_confirmation is not None

# Case 4: explicit confirmation allows resume and action executes.
responses = [
    make_response(
        function_calls=[
            types.FunctionCall(
                name="click_at",
                args={
                    "x": 10,
                    "y": 20,
                    "safety_decision": {
                        "decision": "require_confirmation",
                        "explanation": "Sensitive action",
                    },
                },
            )
        ]
    ),
    make_response(text="done after confirmation"),
]
computer = FakeComputer()
worker = ComputerUseWorker(computer=computer, model_client=FakeModelClient(responses))
paused = worker.run(query="resume path", max_steps=4)
pending = paused.pending_confirmation
assert paused.status == "awaiting_confirmation", paused
assert pending is not None
resumed = worker.resume_after_confirmation(
    confirmation_id=pending.confirmation_id,
    acknowledged=True,
)
assert resumed.status == "completed", resumed
assert computer.clicked_positions == [(10, 20)], computer.clicked_positions

# Case 5: explicit denial terminates safely.
responses = [
    make_response(
        function_calls=[
            types.FunctionCall(
                name="click_at",
                args={
                    "x": 10,
                    "y": 20,
                    "safety_decision": {
                        "decision": "require_confirmation",
                        "explanation": "Sensitive action",
                    },
                },
            )
        ]
    ),
]
computer = FakeComputer()
worker = ComputerUseWorker(computer=computer, model_client=FakeModelClient(responses))
paused = worker.run(query="deny path", max_steps=3)
pending = paused.pending_confirmation
assert pending is not None
denied = worker.resume_after_confirmation(
    confirmation_id=pending.confirmation_id,
    acknowledged=False,
)
assert denied.status == "safety_denied", denied
assert computer.clicked_positions == [], computer.clicked_positions

print("session05_worker_checks=passed")
PY

echo "[2/4] Verifying Computer Use readiness endpoint wiring..."

if [[ ! -x ".venv/bin/uvicorn" ]]; then
  echo "Missing .venv uvicorn executable."
  exit 1
fi

LOG_FILE="$(mktemp)"
cleanup() {
  if [[ -n "${UV_PID:-}" ]] && kill -0 "$UV_PID" 2>/dev/null; then
    kill "$UV_PID" 2>/dev/null || true
    wait "$UV_PID" 2>/dev/null || true
  fi
  rm -f "$LOG_FILE"
}
trap cleanup EXIT

.venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port 8091 >"$LOG_FILE" 2>&1 &
UV_PID=$!

for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:8091/computer-use/health" >/tmp/session05_health.json 2>/dev/null; then
    break
  fi
  sleep 0.5
done

if [[ ! -s /tmp/session05_health.json ]]; then
  echo "Failed to hit /computer-use/health endpoint."
  tail -n 80 "$LOG_FILE" || true
  exit 1
fi

python3 - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("/tmp/session05_health.json").read_text())
assert payload["provider_default"] in {"playwright", "browserbase"}, payload
assert "playwright" in payload["providers"], payload
assert "browserbase" in payload["providers"], payload
assert payload["status"] in {"ready", "degraded"}, payload
assert isinstance(payload.get("active_runs"), int), payload
print("session05_health_endpoint=passed")
PY

echo "[3/4] Verifying Computer Use safety response endpoint wiring..."

HTTP_CODE="$(
  curl -sS -o /tmp/session05_safety_response.json -w "%{http_code}" \
    -X POST "http://127.0.0.1:8091/computer-use/safety-response" \
    -H "Content-Type: application/json" \
    -d '{"run_id":"missing-run","confirmation_id":"missing-confirmation","acknowledged":true}'
)"

if [[ "$HTTP_CODE" != "404" ]]; then
  echo "Expected 404 from /computer-use/safety-response for missing run, got: $HTTP_CODE"
  cat /tmp/session05_safety_response.json || true
  exit 1
fi

python3 - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("/tmp/session05_safety_response.json").read_text())
detail = str(payload.get("detail", ""))
assert "No active run found for run_id." in detail, payload
print("session05_safety_response_endpoint=passed")
PY

echo "[4/4] Session 05 foundation verification complete."
echo "session05_verification=passed"
