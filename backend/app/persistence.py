"""Session 09 persistence helpers (local JSON + optional Firestore mirror)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

LOGGER = logging.getLogger("cloudtutor.persistence")
SAFE_COMPONENT_PATTERN = re.compile(r"[^a-zA-Z0-9_-]+")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _safe_component(value: str, fallback: str) -> str:
    sanitized = SAFE_COMPONENT_PATTERN.sub("-", value.strip())[:72].strip("-")
    return sanitized or fallback


def _session_storage_key(user_id: str, session_id: str) -> str:
    digest = hashlib.sha1(f"{user_id}:{session_id}".encode("utf-8")).hexdigest()[:14]
    safe_user = _safe_component(user_id, "user")
    safe_session = _safe_component(session_id, "session")
    return f"{safe_user}--{safe_session}--{digest}"


class SessionCitation(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    url: str = Field(min_length=1, max_length=2000)


class SessionEvent(BaseModel):
    event_id: str
    time_utc: str
    role: str
    event_type: str
    text: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    citations: list[SessionCitation] = Field(default_factory=list)


class SessionSnapshotResponse(BaseModel):
    user_id: str
    session_id: str
    created_at: str
    updated_at: str
    dialogue_state: dict[str, Any] = Field(default_factory=dict)
    user_transcript: str = ""
    agent_transcript: str = ""
    citations: list[SessionCitation] = Field(default_factory=list)
    event_count: int = 0
    last_event_type: str | None = None
    storage_key: str


class SessionPersistenceManager:
    """Durable session snapshots with optional Firestore mirroring."""

    def __init__(
        self,
        *,
        base_dir: Path,
        transcript_limit: int = 120_000,
        enable_firestore: bool = False,
        firestore_project: str | None = None,
        firestore_collection: str = "cloudtutor_sessions",
    ) -> None:
        self._base_dir = base_dir
        self._snapshot_dir = self._base_dir / "snapshots"
        self._events_dir = self._base_dir / "events"
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._events_dir.mkdir(parents=True, exist_ok=True)
        self._transcript_limit = max(10_000, transcript_limit)
        self._lock = threading.RLock()
        self._cache: dict[str, SessionSnapshotResponse] = {}

        self._firestore_collection_ref: Any | None = None
        self._firestore_disabled_reason: str | None = None
        self._firestore_failure_count = 0
        self._firestore_failure_limit = max(
            1, int(os.getenv("CLOUDTUTOR_FIRESTORE_FAILURE_LIMIT", "5"))
        )
        firestore_requested = enable_firestore or _bool_env(
            "CLOUDTUTOR_FIRESTORE_ENABLED", False
        )
        if firestore_requested:
            try:
                from google.cloud import firestore  # type: ignore

                project_id = (
                    firestore_project
                    or os.getenv("FIRESTORE_PROJECT_ID", "").strip()
                    or os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
                    or None
                )
                client = firestore.Client(project=project_id)
                collection_name = (
                    os.getenv("CLOUDTUTOR_FIRESTORE_COLLECTION", "").strip()
                    or firestore_collection
                )
                self._firestore_collection_ref = client.collection(collection_name)
                self._firestore_failure_count = 0
                LOGGER.info(
                    "Firestore session mirror enabled (collection=%s, project=%s)",
                    collection_name,
                    project_id or "default",
                )
            except Exception as exc:  # noqa: BLE001
                self._firestore_disabled_reason = str(exc)
                LOGGER.warning(
                    "Firestore requested but unavailable. Falling back to local session store only. Reason: %s",
                    exc,
                )

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def firestore_enabled(self) -> bool:
        return self._firestore_collection_ref is not None

    def firestore_status(self) -> dict[str, Any]:
        return {
            "enabled": self.firestore_enabled(),
            "failure_count": self._firestore_failure_count,
            "failure_limit": self._firestore_failure_limit,
            "disabled_reason": self._firestore_disabled_reason,
        }

    def _snapshot_path(self, storage_key: str) -> Path:
        return self._snapshot_dir / f"{storage_key}.json"

    def _events_path(self, storage_key: str) -> Path:
        return self._events_dir / f"{storage_key}.jsonl"

    def _write_snapshot(self, snapshot: SessionSnapshotResponse) -> None:
        payload = snapshot.model_dump()
        path = self._snapshot_path(snapshot.storage_key)
        path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    def _load_snapshot(self, storage_key: str) -> SessionSnapshotResponse | None:
        path = self._snapshot_path(storage_key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return SessionSnapshotResponse(**payload)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed reading session snapshot %s: %s", path, exc)
            return None

    def _ensure_snapshot(
        self,
        *,
        user_id: str,
        session_id: str,
        dialogue_state: dict[str, Any] | None = None,
    ) -> SessionSnapshotResponse:
        storage_key = _session_storage_key(user_id, session_id)
        snapshot = self._cache.get(storage_key)
        if snapshot is None:
            snapshot = self._load_snapshot(storage_key)
            if snapshot is not None:
                self._cache[storage_key] = snapshot
        if snapshot is None:
            now = utc_now_iso()
            snapshot = SessionSnapshotResponse(
                user_id=user_id,
                session_id=session_id,
                created_at=now,
                updated_at=now,
                dialogue_state=dialogue_state or {},
                storage_key=storage_key,
            )
            self._cache[storage_key] = snapshot
            self._write_snapshot(snapshot)
            self._sync_firestore_snapshot(snapshot)
        return snapshot

    def load_session(self, user_id: str, session_id: str) -> SessionSnapshotResponse | None:
        with self._lock:
            storage_key = _session_storage_key(user_id, session_id)
            cached = self._cache.get(storage_key)
            if cached:
                return cached
            loaded = self._load_snapshot(storage_key)
            if loaded:
                self._cache[storage_key] = loaded
            return loaded

    def save_dialogue_state(
        self, user_id: str, session_id: str, dialogue_state: dict[str, Any]
    ) -> SessionSnapshotResponse:
        with self._lock:
            snapshot = self._ensure_snapshot(
                user_id=user_id, session_id=session_id, dialogue_state=dialogue_state
            )
            snapshot.dialogue_state = dict(dialogue_state)
            snapshot.updated_at = utc_now_iso()
            self._write_snapshot(snapshot)
            self._sync_firestore_snapshot(snapshot)
            return snapshot

    def record_user_message(
        self,
        user_id: str,
        session_id: str,
        *,
        text: str,
        dialogue_state: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionSnapshotResponse:
        return self._record_event(
            user_id=user_id,
            session_id=session_id,
            role="user",
            event_type="text",
            text=text,
            dialogue_state=dialogue_state,
            metadata=metadata,
        )

    def record_agent_message(
        self,
        user_id: str,
        session_id: str,
        *,
        text: str,
        citations: list[dict[str, str]] | None = None,
        dialogue_state: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionSnapshotResponse:
        return self._record_event(
            user_id=user_id,
            session_id=session_id,
            role="agent",
            event_type="text",
            text=text,
            citations=citations or [],
            dialogue_state=dialogue_state,
            metadata=metadata,
        )

    def record_system_event(
        self,
        user_id: str,
        session_id: str,
        *,
        event_type: str,
        text: str | None = None,
        dialogue_state: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionSnapshotResponse:
        return self._record_event(
            user_id=user_id,
            session_id=session_id,
            role="system",
            event_type=event_type,
            text=text,
            dialogue_state=dialogue_state,
            metadata=metadata,
        )

    def list_recent_events(
        self, user_id: str, session_id: str, *, limit: int = 40
    ) -> list[SessionEvent]:
        with self._lock:
            storage_key = _session_storage_key(user_id, session_id)
            events_path = self._events_path(storage_key)
            if not events_path.exists():
                return []
            lines = events_path.read_text(encoding="utf-8").splitlines()
            sliced = lines[-max(1, min(500, limit)) :]
            events: list[SessionEvent] = []
            for line in sliced:
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                    events.append(SessionEvent(**payload))
                except Exception:  # noqa: BLE001
                    continue
            return events

    def _record_event(
        self,
        *,
        user_id: str,
        session_id: str,
        role: str,
        event_type: str,
        text: str | None = None,
        citations: list[dict[str, str]] | None = None,
        dialogue_state: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionSnapshotResponse:
        with self._lock:
            snapshot = self._ensure_snapshot(
                user_id=user_id, session_id=session_id, dialogue_state=dialogue_state
            )
            now = utc_now_iso()
            clean_text = (text or "").strip() or None
            merged_citations = self._merge_citations(
                snapshot.citations, citations or []
            )
            if dialogue_state is not None:
                snapshot.dialogue_state = dict(dialogue_state)
            if role == "user" and clean_text:
                snapshot.user_transcript = self._append_transcript(
                    snapshot.user_transcript, now, clean_text
                )
            elif role == "agent" and clean_text:
                snapshot.agent_transcript = self._append_transcript(
                    snapshot.agent_transcript, now, clean_text
                )

            snapshot.citations = merged_citations
            snapshot.event_count += 1
            snapshot.last_event_type = event_type
            snapshot.updated_at = now

            event = SessionEvent(
                event_id=self._build_event_id(
                    user_id=user_id,
                    session_id=session_id,
                    event_index=snapshot.event_count,
                    event_time_utc=now,
                ),
                time_utc=now,
                role=role,
                event_type=event_type,
                text=clean_text,
                metadata=metadata or {},
                citations=self._to_citation_models(citations or []),
            )
            self._append_event_line(snapshot.storage_key, event)
            self._write_snapshot(snapshot)
            self._sync_firestore_snapshot(snapshot)
            self._sync_firestore_event(user_id=user_id, session_id=session_id, event=event)
            return snapshot

    def _append_event_line(self, storage_key: str, event: SessionEvent) -> None:
        events_path = self._events_path(storage_key)
        line = json.dumps(event.model_dump(), ensure_ascii=True)
        with events_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")

    def _append_transcript(self, existing: str, ts_iso: str, message: str) -> str:
        stamp = datetime.fromisoformat(ts_iso).strftime("%H:%M:%S")
        line = f"[{stamp}] {message}\n"
        combined = f"{existing}{line}"
        if len(combined) <= self._transcript_limit:
            return combined
        return combined[-self._transcript_limit :]

    def _build_event_id(
        self,
        *,
        user_id: str,
        session_id: str,
        event_index: int,
        event_time_utc: str,
    ) -> str:
        digest = hashlib.sha1(
            f"{user_id}:{session_id}:{event_index}:{event_time_utc}".encode("utf-8")
        ).hexdigest()[:12]
        return f"evt-{event_index}-{digest}"

    def _merge_citations(
        self, existing: list[SessionCitation], incoming: list[dict[str, str]]
    ) -> list[SessionCitation]:
        deduped: dict[str, SessionCitation] = {}
        for item in existing:
            deduped[item.url] = item
        for item in incoming:
            url = str(item.get("url", "")).strip()
            if not url.startswith(("http://", "https://")):
                continue
            title = str(item.get("title", "")).strip() or url
            deduped[url] = SessionCitation(title=title[:240], url=url[:2000])
        return list(deduped.values())

    def _to_citation_models(self, incoming: list[dict[str, str]]) -> list[SessionCitation]:
        output: list[SessionCitation] = []
        for item in incoming:
            url = str(item.get("url", "")).strip()
            if not url.startswith(("http://", "https://")):
                continue
            title = str(item.get("title", "")).strip() or url
            output.append(SessionCitation(title=title[:240], url=url[:2000]))
        return output

    def _firestore_doc_id(self, user_id: str, session_id: str) -> str:
        return hashlib.sha1(f"{user_id}:{session_id}".encode("utf-8")).hexdigest()

    def _sync_firestore_snapshot(self, snapshot: SessionSnapshotResponse) -> None:
        if self._firestore_collection_ref is None:
            return
        try:
            doc_ref = self._firestore_collection_ref.document(
                self._firestore_doc_id(snapshot.user_id, snapshot.session_id)
            )
            doc_ref.set(snapshot.model_dump(), merge=True)
            self._firestore_failure_count = 0
        except Exception as exc:  # noqa: BLE001
            self._handle_firestore_failure(exc, operation="snapshot")

    def _sync_firestore_event(
        self,
        *,
        user_id: str,
        session_id: str,
        event: SessionEvent,
    ) -> None:
        if self._firestore_collection_ref is None:
            return
        try:
            doc_ref = self._firestore_collection_ref.document(
                self._firestore_doc_id(user_id, session_id)
            )
            doc_ref.collection("events").document(event.event_id).set(
                event.model_dump(), merge=True
            )
            self._firestore_failure_count = 0
        except Exception as exc:  # noqa: BLE001
            self._handle_firestore_failure(exc, operation="event")

    def _handle_firestore_failure(self, exc: Exception, *, operation: str) -> None:
        self._firestore_failure_count += 1
        error_text = str(exc)
        lowered = error_text.lower()
        LOGGER.warning(
            "Failed mirroring session %s to Firestore (%s/%s): %s",
            operation,
            self._firestore_failure_count,
            self._firestore_failure_limit,
            error_text,
        )

        missing_database = "database (default) does not exist" in lowered
        too_many_failures = self._firestore_failure_count >= self._firestore_failure_limit
        if not missing_database and not too_many_failures:
            return

        reason = (
            "Firestore default database is missing in project."
            if missing_database
            else f"Reached failure limit ({self._firestore_failure_limit})."
        )
        self._firestore_collection_ref = None
        self._firestore_disabled_reason = reason
        LOGGER.warning(
            "Firestore mirror disabled for this process. Reason: %s",
            reason,
        )
