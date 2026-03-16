"""Thread-isolated Computer backend wrapper.

Runs a stateful Playwright/Browserbase backend in a dedicated thread so
callers can invoke Computer methods safely from sync or async contexts.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Literal
from typing import Any, Callable

from backend.app.computer_use.computer import Computer, EnvState


@dataclass
class _Command:
    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    response_queue: queue.Queue[tuple[Any | None, BaseException | None]]


class ThreadedComputerBackend(Computer):
    """Wraps a Computer backend and executes all calls in one worker thread."""

    def __init__(
        self,
        *,
        backend_factory: Callable[[], Any],
        startup_timeout_s: float = 25.0,
        call_timeout_s: float = 180.0,
    ) -> None:
        self._backend_factory = backend_factory
        self._startup_timeout_s = max(5.0, startup_timeout_s)
        self._call_timeout_s = max(5.0, call_timeout_s)
        self._commands: queue.Queue[_Command | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self.debug_url: str | None = None

    def __enter__(self) -> "ThreadedComputerBackend":
        if self._thread and self._thread.is_alive():
            return self

        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(
            target=self._worker_loop,
            name="cloudtutor-computer-worker",
            daemon=True,
        )
        self._thread.start()

        if not self._ready.wait(timeout=self._startup_timeout_s):
            raise TimeoutError(
                "Timed out waiting for computer backend thread initialization."
            )
        if self._startup_error is not None:
            raise self._startup_error
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._commands.put(None)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=8.0)
        self._thread = None

    def _worker_loop(self) -> None:
        backend: Any | None = None
        try:
            backend = self._backend_factory()
            backend.__enter__()
            self.debug_url = getattr(backend, "debug_url", None)
        except BaseException as exc:  # noqa: BLE001
            self._startup_error = exc
            self._ready.set()
            return

        self._ready.set()

        while True:
            command = self._commands.get()
            if command is None:
                break
            value: Any | None = None
            error: BaseException | None = None
            try:
                method = getattr(backend, command.method)
                value = method(*command.args, **command.kwargs)
                self.debug_url = getattr(backend, "debug_url", self.debug_url)
            except BaseException as exc:  # noqa: BLE001
                error = exc
            command.response_queue.put((value, error))

        try:
            backend.__exit__(None, None, None)
        except BaseException:
            pass

    def _invoke(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self._thread is None or not self._thread.is_alive():
            raise RuntimeError("Computer backend thread is not running.")
        response_queue: queue.Queue[tuple[Any | None, BaseException | None]] = (
            queue.Queue(maxsize=1)
        )
        self._commands.put(
            _Command(
                method=method,
                args=args,
                kwargs=kwargs,
                response_queue=response_queue,
            )
        )
        try:
            value, error = response_queue.get(timeout=self._call_timeout_s)
        except queue.Empty as exc:
            raise TimeoutError(f"Computer action timed out: {method}") from exc
        if error is not None:
            raise error
        return value

    def screen_size(self) -> tuple[int, int]:
        value = self._invoke("screen_size")
        return tuple(value)  # type: ignore[return-value]

    def open_web_browser(self) -> EnvState:
        return self._invoke("open_web_browser")

    def click_at(self, x: int, y: int) -> EnvState:
        return self._invoke("click_at", x, y)

    def hover_at(self, x: int, y: int) -> EnvState:
        return self._invoke("hover_at", x, y)

    def type_text_at(
        self,
        x: int,
        y: int,
        text: str,
        press_enter: bool,
        clear_before_typing: bool,
    ) -> EnvState:
        return self._invoke(
            "type_text_at",
            x,
            y,
            text,
            press_enter,
            clear_before_typing,
        )

    def scroll_document(
        self, direction: Literal["up", "down", "left", "right"]
    ) -> EnvState:
        return self._invoke("scroll_document", direction)

    def scroll_at(
        self,
        x: int,
        y: int,
        direction: Literal["up", "down", "left", "right"],
        magnitude: int,
    ) -> EnvState:
        return self._invoke("scroll_at", x, y, direction, magnitude)

    def wait_5_seconds(self) -> EnvState:
        return self._invoke("wait_5_seconds")

    def go_back(self) -> EnvState:
        return self._invoke("go_back")

    def go_forward(self) -> EnvState:
        return self._invoke("go_forward")

    def search(self) -> EnvState:
        return self._invoke("search")

    def navigate(self, url: str) -> EnvState:
        return self._invoke("navigate", url)

    def key_combination(self, keys: list[str]) -> EnvState:
        return self._invoke("key_combination", keys)

    def drag_and_drop(
        self, x: int, y: int, destination_x: int, destination_y: int
    ) -> EnvState:
        return self._invoke("drag_and_drop", x, y, destination_x, destination_y)

    def current_state(self) -> EnvState:
        return self._invoke("current_state")
