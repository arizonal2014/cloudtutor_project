"""Computer Use model loop and action executor."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Protocol

from google import genai
from google.genai import types
from google.genai.types import Candidate, Content, FunctionResponse, Part

from backend.app.computer_use.computer import Computer, EnvState

LOGGER = logging.getLogger("cloudtutor.computer_use.worker")

MAX_RECENT_TURNS_WITH_SCREENSHOTS = 3

PREDEFINED_COMPUTER_USE_FUNCTIONS = [
    "open_web_browser",
    "click_at",
    "hover_at",
    "type_text_at",
    "scroll_document",
    "scroll_at",
    "wait_5_seconds",
    "go_back",
    "go_forward",
    "search",
    "navigate",
    "key_combination",
    "drag_and_drop",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _use_vertex() -> bool:
    value = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower()
    return value in {"1", "true", "yes"}


def default_computer_use_model() -> str:
    return os.getenv(
        "CLOUDTUTOR_COMPUTER_USE_MODEL",
        "gemini-2.5-computer-use-preview-10-2025",
    )


def default_computer_use_provider() -> Literal["playwright", "browserbase"]:
    raw = os.getenv("CLOUDTUTOR_COMPUTER_USE_PROVIDER", "").strip().lower()
    if raw == "browserbase":
        return "browserbase"
    if raw == "playwright":
        return "playwright"
    if (
        os.getenv("BROWSERBASE_API_KEY", "").strip()
        and os.getenv("BROWSERBASE_PROJECT_ID", "").strip()
    ):
        return "browserbase"
    return "playwright"


class ModelClient(Protocol):
    def generate_content(
        self,
        *,
        model: str,
        contents: list[Content],
        config: types.GenerateContentConfig,
    ) -> Any:
        """Returns a model response object with .candidates[]."""


class GenAIModelClient:
    """Thin wrapper around google.genai client to simplify testing."""

    def __init__(self) -> None:
        use_vertex = _use_vertex()
        api_key = None
        if not use_vertex:
            api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

        self._client = genai.Client(
            api_key=api_key,
            vertexai=use_vertex,
            project=os.getenv("VERTEXAI_PROJECT"),
            location=os.getenv("VERTEXAI_LOCATION"),
        )

    def generate_content(
        self,
        *,
        model: str,
        contents: list[Content],
        config: types.GenerateContentConfig,
    ) -> Any:
        return self._client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )


@dataclass
class PendingSafetyConfirmation:
    confirmation_id: str
    step_index: int
    action: str
    args: dict[str, Any]
    decision: str
    explanation: str | None = None


@dataclass
class WorkerStep:
    index: int
    action: str
    args: dict[str, Any]
    status: Literal["executed", "unsupported", "blocked_by_safety", "error"]
    url: str | None = None
    error: str | None = None
    safety_decision: str | None = None


@dataclass
class WorkerRunResult:
    status: Literal[
        "completed",
        "max_steps_exceeded",
        "awaiting_confirmation",
        "safety_denied",
        "failed",
    ]
    model: str
    query: str
    final_reasoning: str | None
    steps: list[WorkerStep]
    completed_steps: int
    max_steps: int
    started_at: str
    completed_at: str
    error: str | None = None
    pending_confirmation: PendingSafetyConfirmation | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["steps"] = [asdict(step) for step in self.steps]
        return payload


class ComputerUseWorker:
    """Executes the Gemini Computer Use loop with a provided computer backend."""

    def __init__(
        self,
        *,
        computer: Computer,
        model_name: str | None = None,
        excluded_predefined_functions: list[str] | None = None,
        model_client: ModelClient | None = None,
        max_recent_turns_with_screenshots: int = MAX_RECENT_TURNS_WITH_SCREENSHOTS,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._computer = computer
        self._model_name = model_name or default_computer_use_model()
        self._model_client = model_client or GenAIModelClient()
        self._max_recent_turns_with_screenshots = max_recent_turns_with_screenshots
        self._excluded_predefined_functions = excluded_predefined_functions or []
        self._contents: list[Content] = []

        self._query: str = ""
        self._max_steps: int = 30
        self._started_at: str = utc_now_iso()
        self._step_number: int = 1
        self._steps: list[WorkerStep] = []
        self._final_reasoning: str | None = None
        self._pending_confirmation: PendingSafetyConfirmation | None = None
        self._progress_callback = progress_callback

        self._generate_content_config = types.GenerateContentConfig(
            temperature=0.6,
            top_p=0.95,
            top_k=40,
            max_output_tokens=4096,
            tools=[
                types.Tool(
                    computer_use=types.ComputerUse(
                        environment=types.Environment.ENVIRONMENT_BROWSER,
                        excluded_predefined_functions=self._excluded_predefined_functions,
                    ),
                )
            ],
        )

    def _emit_progress(self, payload: dict[str, Any]) -> None:
        if self._progress_callback is None:
            return
        event_payload = {"time_utc": utc_now_iso(), **payload}
        try:
            self._progress_callback(event_payload)
        except Exception:  # noqa: BLE001
            LOGGER.debug("Progress callback failed.", exc_info=True)

    def run(
        self,
        *,
        query: str,
        max_steps: int = 30,
    ) -> WorkerRunResult:
        self._query = query
        self._max_steps = max_steps
        self._started_at = utc_now_iso()
        self._step_number = 1
        self._steps = []
        self._final_reasoning = None
        self._pending_confirmation = None
        self._contents = [Content(role="user", parts=[Part(text=query)])]
        self._emit_progress(
            {
                "event": "run_started",
                "query": query[:500],
                "max_steps": max_steps,
            }
        )
        return self._continue_loop()

    def resume_after_confirmation(
        self,
        *,
        confirmation_id: str,
        acknowledged: bool,
    ) -> WorkerRunResult:
        pending = self._pending_confirmation
        if pending is None:
            self._emit_progress(
                {
                    "event": "resume_failed",
                    "error": "No pending safety confirmation found.",
                }
            )
            return self._build_result(
                status="failed",
                error="No pending safety confirmation found for this run.",
            )
        if confirmation_id != pending.confirmation_id:
            self._emit_progress(
                {
                    "event": "resume_failed",
                    "error": "Confirmation id mismatch.",
                }
            )
            return self._build_result(
                status="failed",
                error="Confirmation id mismatch for pending safety action.",
                pending_confirmation=pending,
            )

        if not acknowledged:
            self._pending_confirmation = None
            self._steps.append(
                WorkerStep(
                    index=pending.step_index,
                    action=pending.action,
                    args=pending.args,
                    status="blocked_by_safety",
                    safety_decision="denied_by_user",
                    error="User denied safety confirmation.",
                )
            )
            self._emit_progress(
                {
                    "event": "safety_denied_by_user",
                    "step_index": pending.step_index,
                    "action": pending.action,
                }
            )
            return self._build_result(
                status="safety_denied",
                error="User denied safety confirmation.",
            )

        function_response, step = self._execute_action(
            action_name=pending.action,
            action_args=pending.args,
            step_index=pending.step_index,
            extra_response_fields={"safety_acknowledgement": "true"},
        )
        step.safety_decision = "acknowledged_by_user"
        self._steps.append(step)
        self._emit_progress(
            {
                "event": "action_result",
                "step_index": step.index,
                "action": step.action,
                "status": step.status,
                "url": step.url,
                "error": step.error,
            }
        )
        self._contents.append(
            Content(
                role="user",
                parts=[Part(function_response=function_response)],
            )
        )
        self._trim_old_screenshots()

        self._pending_confirmation = None
        self._step_number = pending.step_index + 1
        return self._continue_loop()

    def _continue_loop(self) -> WorkerRunResult:
        while self._step_number <= self._max_steps:
            try:
                self._emit_progress(
                    {
                        "event": "model_thinking",
                        "step_index": self._step_number,
                    }
                )
                response = self._get_model_response()
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Computer Use model call failed.")
                return self._build_result(status="failed", error=str(exc))

            candidate = self._first_candidate(response)
            if candidate is None:
                return self._build_result(
                    status="failed",
                    error="Model returned no candidates.",
                )

            if candidate.content:
                self._contents.append(candidate.content)

            reasoning = self._extract_text(candidate)
            if reasoning:
                self._final_reasoning = reasoning

            function_calls = self._extract_function_calls(candidate)
            if not function_calls:
                self._emit_progress({"event": "model_completed"})
                return self._build_result(status="completed")

            function_responses: list[FunctionResponse] = []
            for function_call in function_calls:
                action_name = function_call.name
                action_args = self._to_args_dict(function_call.args)
                self._emit_progress(
                    {
                        "event": "action_planned",
                        "step_index": self._step_number,
                        "action": action_name,
                        "args": action_args,
                    }
                )
                safety = action_args.get("safety_decision")
                if isinstance(safety, dict):
                    safety_decision = str(safety.get("decision", "")).strip().lower()
                    if safety_decision == "require_confirmation":
                        pending = PendingSafetyConfirmation(
                            confirmation_id=uuid.uuid4().hex,
                            step_index=self._step_number,
                            action=action_name,
                            args=action_args,
                            decision=safety_decision,
                            explanation=str(safety.get("explanation", "")).strip() or None,
                        )
                        self._pending_confirmation = pending
                        self._steps.append(
                            WorkerStep(
                                index=self._step_number,
                                action=action_name,
                                args=action_args,
                                status="blocked_by_safety",
                                safety_decision=safety_decision,
                                error="Action blocked pending explicit user confirmation.",
                            )
                        )
                        self._emit_progress(
                            {
                                "event": "safety_confirmation_required",
                                "step_index": self._step_number,
                                "action": action_name,
                                "args": action_args,
                                "explanation": pending.explanation or "",
                                "confirmation_id": pending.confirmation_id,
                            }
                        )
                        return self._build_result(
                            status="awaiting_confirmation",
                            error="Received require_confirmation safety decision.",
                            pending_confirmation=pending,
                        )

                function_response, step = self._execute_action(
                    action_name=action_name,
                    action_args=action_args,
                    step_index=self._step_number,
                )
                self._steps.append(step)
                self._emit_progress(
                    {
                        "event": "action_result",
                        "step_index": step.index,
                        "action": step.action,
                        "status": step.status,
                        "url": step.url,
                        "error": step.error,
                    }
                )
                function_responses.append(function_response)

            self._contents.append(
                Content(
                    role="user",
                    parts=[Part(function_response=fr) for fr in function_responses],
                )
            )
            self._trim_old_screenshots()
            self._step_number += 1

        return self._build_result(
            status="max_steps_exceeded",
            error=f"Reached max_steps={self._max_steps} before completion.",
        )

    def _build_result(
        self,
        *,
        status: Literal[
            "completed",
            "max_steps_exceeded",
            "awaiting_confirmation",
            "safety_denied",
            "failed",
        ],
        error: str | None = None,
        pending_confirmation: PendingSafetyConfirmation | None = None,
    ) -> WorkerRunResult:
        self._emit_progress(
            {
                "event": "run_result",
                "status": status,
                "completed_steps": len(self._steps),
                "max_steps": self._max_steps,
                "error": error or "",
            }
        )
        return WorkerRunResult(
            status=status,
            model=self._model_name,
            query=self._query,
            final_reasoning=self._final_reasoning,
            steps=list(self._steps),
            completed_steps=len(self._steps),
            max_steps=self._max_steps,
            started_at=self._started_at,
            completed_at=utc_now_iso(),
            error=error,
            pending_confirmation=pending_confirmation,
        )

    def _execute_action(
        self,
        *,
        action_name: str,
        action_args: dict[str, Any],
        step_index: int,
        extra_response_fields: dict[str, Any] | None = None,
    ) -> tuple[FunctionResponse, WorkerStep]:
        extra_response_fields = extra_response_fields or {}
        result, status, error_text, current_url = self._handle_action(
            action_name, action_args
        )

        step = WorkerStep(
            index=step_index,
            action=action_name,
            args=action_args,
            status=status,
            url=current_url,
            error=error_text,
        )

        if isinstance(result, EnvState):
            function_response = FunctionResponse(
                name=action_name,
                response={"url": result.url, **extra_response_fields},
                parts=[
                    types.FunctionResponsePart(
                        inline_data=types.FunctionResponseBlob(
                            mime_type="image/png",
                            data=result.screenshot,
                        )
                    )
                ],
            )
        else:
            function_response = FunctionResponse(
                name=action_name,
                response={**result, **extra_response_fields},
            )

        return function_response, step

    def _get_model_response(self, *, max_retries: int = 4, base_delay_s: float = 1.0) -> Any:
        last_error: Exception | None = None
        for attempt in range(max_retries):
            try:
                return self._model_client.generate_content(
                    model=self._model_name,
                    contents=self._contents,
                    config=self._generate_content_config,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < max_retries - 1:
                    delay = base_delay_s * (2**attempt)
                    time.sleep(delay)
        if last_error is None:
            raise RuntimeError("Unknown model invocation failure.")
        raise last_error

    @staticmethod
    def _first_candidate(response: Any) -> Candidate | Any | None:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return None
        return candidates[0]

    @staticmethod
    def _extract_text(candidate: Candidate | Any) -> str | None:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        text_parts: list[str] = []
        for part in parts:
            text = getattr(part, "text", None)
            if text:
                text_parts.append(text)
        if not text_parts:
            return None
        return " ".join(text_parts).strip()

    @staticmethod
    def _extract_function_calls(candidate: Candidate | Any) -> list[Any]:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        function_calls: list[Any] = []
        for part in parts:
            function_call = getattr(part, "function_call", None)
            if function_call:
                function_calls.append(function_call)
        return function_calls

    @staticmethod
    def _to_args_dict(raw_args: Any) -> dict[str, Any]:
        if raw_args is None:
            return {}
        if isinstance(raw_args, dict):
            return dict(raw_args)
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                return {"value": raw_args}
            return {"value": raw_args}
        if hasattr(raw_args, "items"):
            try:
                return {str(key): value for key, value in raw_args.items()}
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def _handle_action(
        self,
        action_name: str,
        action_args: dict[str, Any],
    ) -> tuple[EnvState | dict[str, Any], Literal["executed", "unsupported", "error"], str | None, str | None]:
        if action_name in self._excluded_predefined_functions:
            error_text = f"Action '{action_name}' is excluded by configuration."
            return {"error": error_text}, "unsupported", error_text, None

        try:
            if action_name == "open_web_browser":
                state = self._computer.open_web_browser()
            elif action_name == "click_at":
                state = self._computer.click_at(
                    x=self.denormalize_x(action_args.get("x", 0)),
                    y=self.denormalize_y(action_args.get("y", 0)),
                )
            elif action_name == "hover_at":
                state = self._computer.hover_at(
                    x=self.denormalize_x(action_args.get("x", 0)),
                    y=self.denormalize_y(action_args.get("y", 0)),
                )
            elif action_name == "type_text_at":
                state = self._computer.type_text_at(
                    x=self.denormalize_x(action_args.get("x", 0)),
                    y=self.denormalize_y(action_args.get("y", 0)),
                    text=str(action_args.get("text", "")),
                    press_enter=bool(action_args.get("press_enter", False)),
                    clear_before_typing=bool(action_args.get("clear_before_typing", True)),
                )
            elif action_name == "scroll_document":
                state = self._computer.scroll_document(
                    str(action_args.get("direction", "down"))  # type: ignore[arg-type]
                )
            elif action_name == "scroll_at":
                direction = str(action_args.get("direction", "down"))
                magnitude = int(action_args.get("magnitude", 800))
                if direction in {"up", "down"}:
                    magnitude = self.denormalize_y(magnitude)
                elif direction in {"left", "right"}:
                    magnitude = self.denormalize_x(magnitude)
                state = self._computer.scroll_at(
                    x=self.denormalize_x(action_args.get("x", 0)),
                    y=self.denormalize_y(action_args.get("y", 0)),
                    direction=direction,  # type: ignore[arg-type]
                    magnitude=magnitude,
                )
            elif action_name == "wait_5_seconds":
                state = self._computer.wait_5_seconds()
            elif action_name == "go_back":
                state = self._computer.go_back()
            elif action_name == "go_forward":
                state = self._computer.go_forward()
            elif action_name == "search":
                state = self._computer.search()
            elif action_name == "navigate":
                state = self._computer.navigate(str(action_args.get("url", "")))
            elif action_name == "key_combination":
                keys = str(action_args.get("keys", "")).split("+")
                state = self._computer.key_combination(keys=keys)
            elif action_name == "drag_and_drop":
                state = self._computer.drag_and_drop(
                    x=self.denormalize_x(action_args.get("x", 0)),
                    y=self.denormalize_y(action_args.get("y", 0)),
                    destination_x=self.denormalize_x(action_args.get("destination_x", 0)),
                    destination_y=self.denormalize_y(action_args.get("destination_y", 0)),
                )
            else:
                error_text = f"Unsupported action '{action_name}'."
                return {"error": error_text}, "unsupported", error_text, None

            return state, "executed", None, state.url
        except Exception as exc:  # noqa: BLE001
            error_text = f"{type(exc).__name__}: {exc}"
            LOGGER.exception("Computer action failed: %s", action_name)
            return {"error": error_text}, "error", error_text, None

    def _trim_old_screenshots(self) -> None:
        turns_with_screenshots = 0
        for content in reversed(self._contents):
            if content.role != "user" or not content.parts:
                continue

            has_screenshot = False
            for part in content.parts:
                function_response = part.function_response
                if (
                    function_response
                    and function_response.parts
                    and function_response.name in PREDEFINED_COMPUTER_USE_FUNCTIONS
                ):
                    has_screenshot = True
                    break

            if not has_screenshot:
                continue

            turns_with_screenshots += 1
            if turns_with_screenshots <= self._max_recent_turns_with_screenshots:
                continue

            for part in content.parts:
                function_response = part.function_response
                if (
                    function_response
                    and function_response.parts
                    and function_response.name in PREDEFINED_COMPUTER_USE_FUNCTIONS
                ):
                    function_response.parts = None

    def denormalize_x(self, x: Any) -> int:
        width, _ = self._computer.screen_size()
        numeric = self._safe_numeric(x)
        return max(0, min(int(numeric / 1000 * width), max(width - 1, 0)))

    def denormalize_y(self, y: Any) -> int:
        _, height = self._computer.screen_size()
        numeric = self._safe_numeric(y)
        return max(0, min(int(numeric / 1000 * height), max(height - 1, 0)))

    @staticmethod
    def _safe_numeric(value: Any) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return 0.0
        return 0.0
