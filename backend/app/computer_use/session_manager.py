"""In-memory run session manager for safety confirmation resume flow."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from backend.app.computer_use.browserbase_computer import BrowserbaseComputer
from backend.app.computer_use.playwright_computer import PlaywrightComputer
from backend.app.computer_use.threaded_backend import ThreadedComputerBackend
from backend.app.computer_use.worker import ComputerUseWorker, utc_now_iso


ComputerUseProvider = Literal["playwright", "browserbase"]


@dataclass
class ComputerUseRunSession:
    run_id: str
    provider: ComputerUseProvider
    backend: Any
    worker: ComputerUseWorker
    created_at: str
    updated_at: str


class ComputerUseSessionManager:
    """Stores active Computer Use runs paused on safety confirmation."""

    def __init__(self, *, ttl_seconds: int = 900, max_sessions: int = 32) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_sessions = max_sessions
        self._sessions: dict[str, ComputerUseRunSession] = {}
        self._lock = threading.Lock()

    def create_session(
        self,
        *,
        provider: ComputerUseProvider,
        screen_size: tuple[int, int],
        initial_url: str,
        model_name: str,
        excluded_actions: list[str],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> ComputerUseRunSession:
        backend_cls = BrowserbaseComputer if provider == "browserbase" else PlaywrightComputer
        backend = ThreadedComputerBackend(
            backend_factory=lambda: backend_cls(
                screen_size=screen_size,
                initial_url=initial_url,
            )
        )
        computer = backend.__enter__()
        worker = ComputerUseWorker(
            computer=computer,
            model_name=model_name,
            excluded_predefined_functions=excluded_actions,
            progress_callback=progress_callback,
        )

        now = utc_now_iso()
        session = ComputerUseRunSession(
            run_id=uuid.uuid4().hex,
            provider=provider,
            backend=backend,
            worker=worker,
            created_at=now,
            updated_at=now,
        )

        with self._lock:
            self._evict_expired_locked()
            if len(self._sessions) >= self._max_sessions:
                oldest_run_id = min(
                    self._sessions,
                    key=lambda run_id: self._sessions[run_id].updated_at,
                )
                stale = self._sessions.pop(oldest_run_id)
                self._close_backend(stale.backend)
            self._sessions[session.run_id] = session
        return session

    def get_session(self, run_id: str) -> ComputerUseRunSession | None:
        with self._lock:
            self._evict_expired_locked()
            session = self._sessions.get(run_id)
            if session:
                session.updated_at = utc_now_iso()
            return session

    def close_session(self, run_id: str) -> None:
        backend = None
        with self._lock:
            session = self._sessions.pop(run_id, None)
            if session:
                backend = session.backend
        if backend is not None:
            self._close_backend(backend)

    def active_run_count(self) -> int:
        with self._lock:
            self._evict_expired_locked()
            return len(self._sessions)

    def _evict_expired_locked(self) -> None:
        now_ts = time.time()
        expired: list[str] = []
        for run_id, session in self._sessions.items():
            try:
                if not session.updated_at:
                    updated_ts = now_ts
                else:
                    updated_dt = datetime.fromisoformat(session.updated_at)
                    if updated_dt.tzinfo is None:
                        updated_dt = updated_dt.replace(tzinfo=timezone.utc)
                    updated_ts = updated_dt.timestamp()
            except Exception:
                updated_ts = now_ts
            if now_ts - updated_ts > self._ttl_seconds:
                expired.append(run_id)

        for run_id in expired:
            stale = self._sessions.pop(run_id, None)
            if stale is not None:
                self._close_backend(stale.backend)

    @staticmethod
    def _close_backend(backend: Any) -> None:
        try:
            backend.__exit__(None, None, None)
        except Exception:
            pass
