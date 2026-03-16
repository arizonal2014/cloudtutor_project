"""Session 03 backend: ADK live streaming + tutor flow state machine."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from google.adk.agents import LiveRequestQueue
from google.adk.agents.run_config import RunConfig
from google.adk.runners import InMemoryRunner
from google.genai import types
from google.genai.types import Blob, Content, Part
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect

from backend.app.artifacts import (
    ArtifactCitation,
    ArtifactCloudStorageUploader,
    TutorialArtifactCreateRequest,
    TutorialArtifactCreateResponse,
    TutorialArtifactManager,
)
from backend.app.dialogue_state import (
    TutorDialogueState,
    detect_intent,
    infer_cloud_service_topic,
    infer_topic,
    is_affirmative,
    is_more_details_request,
    is_negative,
    is_next_use_case_request,
    is_resume_request,
    is_stop_use_case_request,
    normalize_text,
    should_ground_query,
)
from backend.app.computer_use import (
    ComputerUseProvider,
    ComputerUseConfigurationError,
    ComputerUseDependencyError,
    ComputerUseHealthResponse,
    ComputerUseRunSession,
    ComputerUseRunRequest,
    ComputerUseRunResponse,
    ComputerUseSafetyResponseRequest,
    ComputerUseSessionManager,
    ComputerUseStep,
    PendingSafetyConfirmationPayload,
    default_computer_use_model,
    default_computer_use_provider,
)
from backend.app.persistence import (
    SessionEvent,
    SessionPersistenceManager,
    SessionSnapshotResponse,
)

LOGGER = logging.getLogger("cloudtutor.backend")
logging.basicConfig(level=logging.INFO)

def _load_environment() -> None:
    """Loads environment from project-level and agent-level .env files."""
    root = Path(__file__).resolve().parents[2]
    root_env = root / ".env"
    agent_env = root / "cloud_tutor_agent" / ".env"

    if root_env.exists():
        load_dotenv(root_env, override=False)
    if agent_env.exists():
        load_dotenv(agent_env, override=False)


_load_environment()
# Import after environment is loaded so model/provider selection reflects .env.
from backend.app.live_agent import root_agent  # noqa: E402

APP_NAME = os.getenv("CLOUDTUTOR_APP_NAME", "cloudtutor-backend")
AUDIO_DOWNLINK_MODE = os.getenv("CLOUDTUTOR_AUDIO_DOWNLINK_MODE", "binary").strip().lower()


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


runner = InMemoryRunner(app_name=APP_NAME, agent=root_agent)
computer_use_session_manager = ComputerUseSessionManager()
artifact_cloud_uploader: ArtifactCloudStorageUploader | None = None
artifact_bucket_name = os.getenv("CLOUDTUTOR_ARTIFACT_GCS_BUCKET", "").strip()
if artifact_bucket_name:
    artifact_cloud_uploader = ArtifactCloudStorageUploader(
        bucket_name=artifact_bucket_name,
        prefix=os.getenv("CLOUDTUTOR_ARTIFACT_GCS_PREFIX", "tutorial-artifacts").strip(),
        project_id=os.getenv("CLOUDTUTOR_ARTIFACT_GCS_PROJECT", "").strip() or None,
    )
artifact_manager = TutorialArtifactManager(
    output_dir=Path(
        os.getenv(
            "CLOUDTUTOR_ARTIFACT_DIR",
            str(Path(__file__).resolve().parents[2] / "docs" / "artifacts"),
        )
    ),
    cloud_uploader=artifact_cloud_uploader,
)
session_persistence_manager = SessionPersistenceManager(
    base_dir=Path(
        os.getenv(
            "CLOUDTUTOR_SESSION_STORE_DIR",
            str(Path(__file__).resolve().parents[2] / "docs" / "sessions"),
        )
    ),
    transcript_limit=max(
        10_000, int(os.getenv("CLOUDTUTOR_SESSION_TRANSCRIPT_LIMIT", "120000"))
    ),
    enable_firestore=_env_bool("CLOUDTUTOR_FIRESTORE_ENABLED", False),
    firestore_project=os.getenv("FIRESTORE_PROJECT_ID", "").strip() or None,
    firestore_collection=os.getenv("CLOUDTUTOR_FIRESTORE_COLLECTION", "cloudtutor_sessions"),
)

app = FastAPI(title="CloudTutor Backend", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_id_and_access_log(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or os.urandom(6).hex()
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - started) * 1000
        LOGGER.exception(
            "http_request_failed method=%s path=%s request_id=%s duration_ms=%.1f",
            request.method,
            request.url.path,
            request_id,
            elapsed_ms,
        )
        raise

    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["x-request-id"] = request_id
    LOGGER.info(
        "http_request method=%s path=%s status=%s request_id=%s duration_ms=%.1f",
        request.method,
        request.url.path,
        response.status_code,
        request_id,
        elapsed_ms,
    )
    return response


def utc_now_iso() -> str:
    """Returns current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


class SessionEventCreateRequest(BaseModel):
    role: str = Field(default="system", pattern="^(user|agent|system)$")
    event_type: str = Field(default="manual_event", min_length=1, max_length=80)
    text: str | None = Field(default=None, max_length=8000)
    citations: list[ArtifactCitation] = Field(default_factory=list)
    dialogue_state: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Health endpoint for local smoke checks."""
    return {
        "status": "ok",
        "service": APP_NAME,
        "time_utc": utc_now_iso(),
        "model": root_agent.model,
        "response_mode": os.getenv("CLOUDTUTOR_RESPONSE_MODE", "audio").strip().lower(),
        "audio_downlink_mode": AUDIO_DOWNLINK_MODE,
        "vad_start_sensitivity": os.getenv("CLOUDTUTOR_VAD_START_SENSITIVITY", "high"),
        "vad_end_sensitivity": os.getenv("CLOUDTUTOR_VAD_END_SENSITIVITY", "high"),
        "vad_prefix_padding_ms": os.getenv("CLOUDTUTOR_VAD_PREFIX_PADDING_MS", "200"),
        "vad_silence_duration_ms": os.getenv("CLOUDTUTOR_VAD_SILENCE_DURATION_MS", "350"),
        "session_store_dir": str(session_persistence_manager.base_dir),
        "firestore_enabled": str(session_persistence_manager.firestore_enabled()).lower(),
        "firestore_status": session_persistence_manager.firestore_status(),
        "artifact_gcs_enabled": str(
            bool(artifact_cloud_uploader and artifact_cloud_uploader.ready)
        ).lower(),
    }


@app.post("/artifacts/tutorial", response_model=TutorialArtifactCreateResponse)
async def create_tutorial_artifact(
    request: TutorialArtifactCreateRequest,
) -> TutorialArtifactCreateResponse:
    """Creates a tutorial artifact (HTML + optional PDF) from session transcripts."""
    try:
        effective_request = request
        snapshot: SessionSnapshotResponse | None = None
        try:
            snapshot = session_persistence_manager.load_session(
                user_id=request.user_id,
                session_id=request.session_id,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to load session snapshot for artifact merge: %s", exc)
        if snapshot is not None:
            merged_user_transcript = (
                request.user_transcript.strip() or snapshot.user_transcript
            )
            merged_agent_transcript = (
                request.agent_transcript.strip() or snapshot.agent_transcript
            )

            citations_by_url: dict[str, ArtifactCitation] = {}
            for citation in request.citations:
                citations_by_url[citation.url] = citation
            for citation in snapshot.citations:
                citations_by_url[citation.url] = ArtifactCitation(
                    title=citation.title,
                    url=citation.url,
                )

            if (
                merged_user_transcript != request.user_transcript
                or merged_agent_transcript != request.agent_transcript
                or len(citations_by_url) != len(request.citations)
            ):
                effective_request = request.model_copy(
                    update={
                        "user_transcript": merged_user_transcript,
                        "agent_transcript": merged_agent_transcript,
                        "citations": list(citations_by_url.values()),
                    }
                )

        return artifact_manager.create_artifact(effective_request)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"Tutorial artifact generation failed: {exc}"
        ) from exc


@app.get("/artifacts/{artifact_id}/html")
async def download_tutorial_artifact_html(artifact_id: str) -> FileResponse:
    """Downloads the generated tutorial HTML artifact."""
    html_path = artifact_manager.get_html_path(artifact_id)
    if html_path is None:
        raise HTTPException(status_code=404, detail="Artifact HTML not found.")
    return FileResponse(
        path=str(html_path),
        media_type="text/html; charset=utf-8",
        filename=f"{artifact_id}.html",
    )


@app.get("/artifacts/{artifact_id}/pdf")
async def download_tutorial_artifact_pdf(artifact_id: str) -> FileResponse:
    """Downloads the generated tutorial PDF artifact when available."""
    pdf_path = artifact_manager.get_pdf_path(artifact_id)
    if pdf_path is None:
        raise HTTPException(status_code=404, detail="Artifact PDF not found.")
    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=f"{artifact_id}.pdf",
    )


@app.get("/artifacts/recent", response_model=list[TutorialArtifactCreateResponse])
async def list_recent_tutorial_artifacts(limit: int = 20) -> list[TutorialArtifactCreateResponse]:
    """Lists recent tutorial artifacts, including persisted records loaded after restart."""
    return artifact_manager.list_recent(limit=limit)


@app.get("/sessions/{user_id}/{session_id}", response_model=SessionSnapshotResponse)
async def get_session_snapshot(user_id: str, session_id: str) -> SessionSnapshotResponse:
    """Returns the durable session snapshot used for resume context."""
    snapshot = session_persistence_manager.load_session(user_id=user_id, session_id=session_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Session snapshot not found.")
    return snapshot


@app.get(
    "/sessions/{user_id}/{session_id}/events",
    response_model=list[SessionEvent],
)
async def get_session_events(
    user_id: str,
    session_id: str,
    limit: int = 40,
) -> list[SessionEvent]:
    """Returns recent persisted session events."""
    return session_persistence_manager.list_recent_events(
        user_id=user_id,
        session_id=session_id,
        limit=limit,
    )


@app.post(
    "/sessions/{user_id}/{session_id}/events",
    response_model=SessionSnapshotResponse,
)
async def append_session_event(
    user_id: str,
    session_id: str,
    request: SessionEventCreateRequest,
) -> SessionSnapshotResponse:
    """Appends a durable session event (useful for deterministic seeding and tests)."""
    try:
        dialogue_state = request.dialogue_state or None
        metadata = request.metadata or {}
        text = (request.text or "").strip()
        citations = [citation.model_dump() for citation in request.citations]

        if request.role == "user":
            return session_persistence_manager.record_user_message(
                user_id=user_id,
                session_id=session_id,
                text=text,
                dialogue_state=dialogue_state,
                metadata={"event_type": request.event_type, **metadata},
            )

        if request.role == "agent":
            return session_persistence_manager.record_agent_message(
                user_id=user_id,
                session_id=session_id,
                text=text,
                citations=citations,
                dialogue_state=dialogue_state,
                metadata={"event_type": request.event_type, **metadata},
            )

        return session_persistence_manager.record_system_event(
            user_id=user_id,
            session_id=session_id,
            event_type=request.event_type,
            text=text or None,
            dialogue_state=dialogue_state,
            metadata=metadata,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"Failed to append session event: {exc}"
        ) from exc


def _module_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def _vertex_mode_enabled() -> bool:
    value = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower()
    return value in {"1", "true", "yes"}


@app.get("/computer-use/health", response_model=ComputerUseHealthResponse)
async def computer_use_health() -> ComputerUseHealthResponse:
    """Readiness endpoint for Computer Use provider backends."""
    common_deps = {"playwright": _module_available("playwright")}
    browserbase_deps = {
        **common_deps,
        "browserbase_sdk": _module_available("browserbase"),
    }
    common_env = {
        "api_key": bool(
            os.getenv("GEMINI_API_KEY", "").strip()
            or os.getenv("GOOGLE_API_KEY", "").strip()
        ),
        "vertex_mode": _vertex_mode_enabled(),
    }
    browserbase_env = {
        **common_env,
        "browserbase_api_key": bool(os.getenv("BROWSERBASE_API_KEY", "").strip()),
        "browserbase_project_id": bool(os.getenv("BROWSERBASE_PROJECT_ID", "").strip()),
    }
    playwright_env = dict(common_env)

    provider_default = default_computer_use_provider()
    notes: list[str] = [
        "Install Chromium once: .venv/bin/playwright install chromium",
        (
            "Default provider is Browserbase when Browserbase credentials are configured."
            if provider_default == "browserbase"
            else "Default provider is Playwright when Browserbase is not configured."
        ),
    ]

    if not common_deps["playwright"]:
        notes.append("Install Playwright package: .venv/bin/pip install playwright")
    if not browserbase_deps["browserbase_sdk"]:
        notes.append("Install Browserbase SDK: .venv/bin/pip install browserbase")

    if not common_env["api_key"] and not common_env["vertex_mode"]:
        notes.append("Set GEMINI_API_KEY (or GOOGLE_API_KEY) or configure Vertex AI env vars.")
    if not browserbase_env["browserbase_api_key"] or not browserbase_env["browserbase_project_id"]:
        notes.append("Set Browserbase env vars for browserbase mode: BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID")

    playwright_ready = common_deps["playwright"] and (common_env["api_key"] or common_env["vertex_mode"])
    browserbase_ready = (
        all(browserbase_deps.values())
        and (common_env["api_key"] or common_env["vertex_mode"])
        and browserbase_env["browserbase_api_key"]
        and browserbase_env["browserbase_project_id"]
    )
    default_ready = playwright_ready if provider_default == "playwright" else browserbase_ready

    return ComputerUseHealthResponse(
        status="ready" if default_ready else "degraded",
        provider_default=provider_default,
        providers={
            "playwright": {
                "ready": playwright_ready,
                "dependencies": common_deps,
                "env": playwright_env,
            },
            "browserbase": {
                "ready": browserbase_ready,
                "dependencies": browserbase_deps,
                "env": browserbase_env,
            },
        },
        model_default=default_computer_use_model(),
        active_runs=computer_use_session_manager.active_run_count(),
        notes=notes,
    )


def _worker_result_to_response(
    *,
    provider: ComputerUseProvider,
    query: str,
    run_id: str | None,
    debug_url: str | None,
    result: Any,
) -> ComputerUseRunResponse:
    pending = result.pending_confirmation
    pending_payload = (
        PendingSafetyConfirmationPayload(**asdict(pending)) if pending else None
    )
    return ComputerUseRunResponse(
        status=result.status,
        model=result.model,
        provider=provider,
        query=query,
        run_id=run_id,
        final_reasoning=result.final_reasoning,
        completed_steps=result.completed_steps,
        max_steps=result.max_steps,
        debug_url=debug_url,
        steps=[ComputerUseStep(**step.__dict__) for step in result.steps],
        pending_confirmation=pending_payload,
        started_at=result.started_at,
        completed_at=result.completed_at,
        error=result.error,
    )


def _create_computer_use_session(
    request: ComputerUseRunRequest,
    progress_callback: Any | None = None,
) -> ComputerUseRunSession:
    return computer_use_session_manager.create_session(
        provider=request.provider,
        screen_size=(request.screen_width, request.screen_height),
        initial_url=request.initial_url,
        model_name=request.model,
        excluded_actions=request.excluded_actions,
        progress_callback=progress_callback,
    )


def _execute_computer_use_session(
    *,
    session: ComputerUseRunSession,
    request: ComputerUseRunRequest,
    keep_session_open: bool = False,
) -> ComputerUseRunResponse:
    try:
        result = session.worker.run(query=request.query, max_steps=request.max_steps)
        debug_url = getattr(session.backend, "debug_url", None)

        should_keep_open = keep_session_open or result.status == "awaiting_confirmation"

        if should_keep_open:
            return _worker_result_to_response(
                provider=session.provider,
                query=request.query,
                run_id=session.run_id,
                debug_url=debug_url,
                result=result,
            )

        computer_use_session_manager.close_session(session.run_id)
        return _worker_result_to_response(
            provider=session.provider,
            query=request.query,
            run_id=None,
            debug_url=debug_url,
            result=result,
        )
    except Exception:
        if not keep_session_open:
            computer_use_session_manager.close_session(session.run_id)
        raise


@app.post("/computer-use/run", response_model=ComputerUseRunResponse)
def computer_use_run(request: ComputerUseRunRequest) -> ComputerUseRunResponse:
    """Runs a Computer Use task using the selected browser backend."""

    def _run_worker() -> ComputerUseRunResponse:
        session = _create_computer_use_session(request)
        return _execute_computer_use_session(session=session, request=request)

    try:
        return _run_worker()
    except (ComputerUseDependencyError, ComputerUseConfigurationError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"Computer Use execution failed: {exc}",
        ) from exc


@app.post("/computer-use/safety-response", response_model=ComputerUseRunResponse)
def computer_use_safety_response(
    request: ComputerUseSafetyResponseRequest,
) -> ComputerUseRunResponse:
    """Approves or denies a pending Computer Use safety confirmation."""

    def _resume_worker() -> ComputerUseRunResponse:
        session: ComputerUseRunSession | None = computer_use_session_manager.get_session(
            request.run_id
        )
        if session is None:
            raise HTTPException(status_code=404, detail="No active run found for run_id.")

        result = session.worker.resume_after_confirmation(
            confirmation_id=request.confirmation_id,
            acknowledged=request.acknowledged,
        )
        debug_url = getattr(session.backend, "debug_url", None)

        keep_session_open = (
            request.keep_session_open
            or result.status == "awaiting_confirmation"
            or result.pending_confirmation is not None
        )
        if keep_session_open:
            return _worker_result_to_response(
                provider=session.provider,
                query=result.query,
                run_id=session.run_id,
                debug_url=debug_url,
                result=result,
            )

        computer_use_session_manager.close_session(session.run_id)
        return _worker_result_to_response(
            provider=session.provider,
            query=result.query,
            run_id=None,
            debug_url=debug_url,
            result=result,
        )

    try:
        return _resume_worker()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"Computer Use safety response handling failed: {exc}",
        ) from exc


def _is_native_audio_model(model_name: str) -> bool:
    return "native-audio" in model_name.lower()


URL_PATTERN = re.compile(r"https?://[^\s<>()\"']+")
MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def _normalize_url(candidate: str) -> str:
    url = candidate.strip()
    while url and url[-1] in ".,);]}>\"'":
        url = url[:-1]
    return url


def _extract_citations_from_text(text: str) -> list[dict[str, str]]:
    citations: list[dict[str, str]] = []
    seen: set[str] = set()

    for title, url in MARKDOWN_LINK_PATTERN.findall(text):
        normalized = _normalize_url(url)
        if normalized.startswith(("http://", "https://")) and normalized not in seen:
            seen.add(normalized)
            citations.append({"title": title.strip() or normalized, "url": normalized})

    for url in URL_PATTERN.findall(text):
        normalized = _normalize_url(url)
        if normalized.startswith(("http://", "https://")) and normalized not in seen:
            seen.add(normalized)
            citations.append({"title": normalized, "url": normalized})

    return citations


def _extract_citations_from_structure(payload: Any, limit: int = 24) -> list[dict[str, str]]:
    citations: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(url_candidate: Any, title_candidate: Any = None) -> None:
        if not isinstance(url_candidate, str):
            return
        normalized = _normalize_url(url_candidate)
        if not normalized.startswith(("http://", "https://")):
            return
        if normalized in seen:
            return
        seen.add(normalized)
        title = title_candidate if isinstance(title_candidate, str) else ""
        citations.append({"title": title.strip() or normalized, "url": normalized})

    def walk(value: Any) -> None:
        if len(citations) >= limit:
            return

        if isinstance(value, dict):
            title_candidate = value.get("title") or value.get("name") or value.get("label")
            for key in ("url", "uri", "link", "href", "source_url", "sourceUri"):
                add(value.get(key), title_candidate)

            web_value = value.get("web")
            if isinstance(web_value, dict):
                add(web_value.get("uri") or web_value.get("url"), web_value.get("title"))

            retrieved_context = value.get("retrieved_context")
            if isinstance(retrieved_context, dict):
                add(
                    retrieved_context.get("uri") or retrieved_context.get("url"),
                    retrieved_context.get("title"),
                )

            for nested in value.values():
                walk(nested)
            return

        if isinstance(value, list):
            for item in value:
                walk(item)
            return

        if isinstance(value, str):
            for found in URL_PATTERN.findall(value):
                add(found)

    walk(payload)
    return citations


def _merge_citations(*citation_sets: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen: set[str] = set()

    for citation_set in citation_sets:
        for item in citation_set:
            url = item.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append({"title": item.get("title", url), "url": url})
    return merged


def _fallback_citations_for_topic(topic: str) -> list[dict[str, str]]:
    normalized = topic.strip().lower()
    if not normalized:
        return [
            {
                "title": "Google Cloud Documentation",
                "url": "https://cloud.google.com/docs",
            }
        ]

    fallback_map: list[tuple[tuple[str, ...], list[dict[str, str]]]] = [
        (
            ("cloud run",),
            [
                {
                    "title": "Cloud Run Pricing",
                    "url": "https://cloud.google.com/run/pricing",
                },
                {
                    "title": "Cloud Run Overview",
                    "url": "https://cloud.google.com/run/docs/overview/what-is-cloud-run",
                },
            ],
        ),
        (
            ("gke", "kubernetes engine"),
            [
                {
                    "title": "GKE Versioning and Upgrades",
                    "url": "https://cloud.google.com/kubernetes-engine/docs/concepts/release-channels",
                },
                {
                    "title": "GKE Documentation",
                    "url": "https://cloud.google.com/kubernetes-engine/docs",
                },
            ],
        ),
        (
            ("firebase hosting",),
            [
                {
                    "title": "Firebase Hosting Pricing",
                    "url": "https://firebase.google.com/pricing",
                },
                {
                    "title": "Firebase Hosting Documentation",
                    "url": "https://firebase.google.com/docs/hosting",
                },
            ],
        ),
        (
            ("bigquery",),
            [
                {
                    "title": "BigQuery Pricing",
                    "url": "https://cloud.google.com/bigquery/pricing",
                },
                {
                    "title": "BigQuery Documentation",
                    "url": "https://cloud.google.com/bigquery/docs",
                },
            ],
        ),
        (
            ("firestore",),
            [
                {
                    "title": "Firestore Pricing",
                    "url": "https://firebase.google.com/pricing",
                },
                {
                    "title": "Firestore Documentation",
                    "url": "https://firebase.google.com/docs/firestore",
                },
            ],
        ),
        (
            ("cloud storage",),
            [
                {
                    "title": "Cloud Storage Pricing",
                    "url": "https://cloud.google.com/storage/pricing",
                },
                {
                    "title": "Cloud Storage Documentation",
                    "url": "https://cloud.google.com/storage/docs",
                },
            ],
        ),
        (
            ("cloud build",),
            [
                {
                    "title": "Cloud Build Pricing",
                    "url": "https://cloud.google.com/build/pricing",
                },
                {
                    "title": "Cloud Build Documentation",
                    "url": "https://cloud.google.com/build/docs",
                },
            ],
        ),
        (
            ("vertex ai",),
            [
                {
                    "title": "Vertex AI Pricing",
                    "url": "https://cloud.google.com/vertex-ai/pricing",
                },
                {
                    "title": "Vertex AI Documentation",
                    "url": "https://cloud.google.com/vertex-ai/docs",
                },
            ],
        ),
    ]

    for keywords, citations in fallback_map:
        if any(keyword in normalized for keyword in keywords):
            return citations

    return [
        {"title": "Google Cloud Pricing", "url": "https://cloud.google.com/pricing"},
        {"title": "Google Cloud Documentation", "url": "https://cloud.google.com/docs"},
    ]


def _start_sensitivity_from_env(value: str) -> types.StartSensitivity:
    normalized = value.strip().lower()
    if normalized in {"high", "start_sensitivity_high"}:
        return types.StartSensitivity.START_SENSITIVITY_HIGH
    return types.StartSensitivity.START_SENSITIVITY_LOW


def _end_sensitivity_from_env(value: str) -> types.EndSensitivity:
    normalized = value.strip().lower()
    if normalized in {"low", "end_sensitivity_low"}:
        return types.EndSensitivity.END_SENSITIVITY_LOW
    return types.EndSensitivity.END_SENSITIVITY_HIGH


def _build_run_config() -> RunConfig:
    """Creates run config based on configured model capabilities."""
    model_name = root_agent.model
    response_mode = os.getenv("CLOUDTUTOR_RESPONSE_MODE", "audio").strip().lower()
    vad_start_sensitivity = _start_sensitivity_from_env(
        os.getenv("CLOUDTUTOR_VAD_START_SENSITIVITY", "high")
    )
    vad_end_sensitivity = _end_sensitivity_from_env(
        os.getenv("CLOUDTUTOR_VAD_END_SENSITIVITY", "high")
    )
    vad_prefix_padding_ms = max(
        0, int(os.getenv("CLOUDTUTOR_VAD_PREFIX_PADDING_MS", "200"))
    )
    vad_silence_duration_ms = max(
        150, int(os.getenv("CLOUDTUTOR_VAD_SILENCE_DURATION_MS", "350"))
    )

    if _is_native_audio_model(model_name):
        use_text_mode = response_mode == "text"
        run_config_kwargs: dict[str, Any] = {
            "streaming_mode": "bidi",
            "session_resumption": types.SessionResumptionConfig(),
            "realtime_input_config": types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    start_of_speech_sensitivity=vad_start_sensitivity,
                    # Balance trailing-word retention with timely turn completion.
                    end_of_speech_sensitivity=vad_end_sensitivity,
                    prefix_padding_ms=vad_prefix_padding_ms,
                    silence_duration_ms=vad_silence_duration_ms,
                )
            ),
            "response_modalities": (
                [types.Modality.TEXT]
                if use_text_mode
                else [types.Modality.AUDIO]
            ),
        }

        if use_text_mode:
            return RunConfig(**run_config_kwargs)

        return RunConfig(
            **run_config_kwargs,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=os.getenv("CLOUDTUTOR_AGENT_VOICE", "Puck")
                    )
                ),
                language_code=os.getenv("CLOUDTUTOR_AGENT_LANGUAGE", "en-US"),
            ),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            input_audio_transcription=types.AudioTranscriptionConfig(),
        )

    # Text fallback for non-native models.
    return RunConfig(
        streaming_mode="bidi",
        session_resumption=types.SessionResumptionConfig(),
        response_modalities=[types.Modality.TEXT],
    )


def _allow_manual_activity_signals(run_config: RunConfig) -> bool:
    """Returns True only when automatic VAD is explicitly disabled."""
    realtime_input = getattr(run_config, "realtime_input_config", None)
    if not realtime_input:
        return False
    aad = getattr(realtime_input, "automatic_activity_detection", None)
    if not aad:
        return False
    return bool(getattr(aad, "disabled", False))


async def _get_or_create_session(user_id: str, session_id: str):
    session = await runner.session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )
    if session:
        return session
    return await runner.session_service.create_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )


def _build_event_message(event: Any) -> dict[str, Any] | None:
    """Converts ADK live events to frontend-friendly envelopes."""
    def _coalesce_attr(obj: Any, *names: str) -> Any:
        for name in names:
            if hasattr(obj, name):
                value = getattr(obj, name)
                if value is not None:
                    return value
        return None

    def _read_transcription(transcription: Any) -> tuple[str | None, bool | None]:
        if transcription is None:
            return None, None
        if isinstance(transcription, dict):
            return transcription.get("text"), transcription.get("finished")
        return (
            getattr(transcription, "text", None),
            getattr(transcription, "finished", None),
        )

    def _normalize_inline_audio_data(data: Any) -> str | None:
        if isinstance(data, bytes):
            return base64.b64encode(data).decode("ascii")
        if isinstance(data, str):
            return data
        return None

    partial = bool(_coalesce_attr(event, "partial") or False)
    turn_complete = bool(_coalesce_attr(event, "turn_complete", "turnComplete") or False)
    interrupted = bool(_coalesce_attr(event, "interrupted") or False)

    message: dict[str, Any] = {
        "type": "agent_event",
        "author": event.author or "agent",
        "is_partial": partial,
        "turn_complete": turn_complete,
        "interrupted": interrupted,
        "error_code": _coalesce_attr(event, "error_code", "errorCode"),
        "error_message": _coalesce_attr(event, "error_message", "errorMessage"),
        "parts": [],
        "input_transcription": None,
        "output_transcription": None,
        "citations": [],
        "time_utc": utc_now_iso(),
    }

    input_transcription_obj = _coalesce_attr(event, "input_transcription", "inputTranscription")
    input_text, input_finished = _read_transcription(input_transcription_obj)
    if input_text:
        message["input_transcription"] = {
            "text": input_text,
            "is_final": bool(input_finished) if input_finished is not None else not partial,
        }

    output_transcription_obj = _coalesce_attr(
        event, "output_transcription", "outputTranscription"
    )
    output_text, output_finished = _read_transcription(output_transcription_obj)
    if output_text:
        message["output_transcription"] = {
            "text": output_text,
            "is_final": bool(output_finished) if output_finished is not None else not partial,
        }
        message["parts"].append({"type": "text", "data": output_text})
        message["citations"] = _merge_citations(
            message["citations"], _extract_citations_from_text(output_text)
        )

    content = getattr(event, "content", None)
    if content:
        text_parts = [part.text for part in content.parts if getattr(part, "text", None)]
        role = getattr(content, "role", "") or ""
        content_text = "".join(text_parts)

        if content_text:
            if role == "user" and not message["input_transcription"]:
                message["input_transcription"] = {
                    "text": content_text,
                    "is_final": not partial,
                }
            elif role != "user" and not message["output_transcription"]:
                message["parts"].append(
                    {
                        "type": "text",
                        "data": content_text,
                    }
                )
                message["output_transcription"] = {
                    "text": content_text,
                    "is_final": not partial,
                }
                message["citations"] = _merge_citations(
                    message["citations"], _extract_citations_from_text(content_text)
                )

        for part in content.parts:
            inline_data = _coalesce_attr(part, "inline_data", "inlineData")
            if inline_data:
                mime_type = _coalesce_attr(inline_data, "mime_type", "mimeType")
                raw_audio = getattr(inline_data, "data", None)
                if mime_type and mime_type.startswith("audio/pcm"):
                    if AUDIO_DOWNLINK_MODE == "binary" and isinstance(raw_audio, bytes):
                        message["parts"].append(
                            {
                                "type": "audio/pcm",
                                "mime_type": mime_type,
                                "stream": "binary",
                                "byte_length": len(raw_audio),
                            }
                        )
                    else:
                        audio_data = _normalize_inline_audio_data(raw_audio)
                        if audio_data:
                            message["parts"].append(
                                {
                                    "type": "audio/pcm",
                                    "mime_type": mime_type,
                                    "stream": "base64",
                                    "data": audio_data,
                                }
                            )
                    continue

            function_call = getattr(part, "function_call", None)
            if function_call:
                message["parts"].append(
                    {
                        "type": "function_call",
                        "data": {
                            "name": function_call.name,
                            "args": function_call.args or {},
                        },
                    }
                )
                continue

            function_response = getattr(part, "function_response", None)
            if function_response:
                response_payload = function_response.response or {}
                message["parts"].append(
                    {
                        "type": "function_response",
                        "data": {
                            "name": function_response.name,
                            "response": response_payload,
                        },
                    }
                )
                message["citations"] = _merge_citations(
                    message["citations"], _extract_citations_from_structure(response_payload)
                )

    if (
        message["parts"]
        or message["input_transcription"]
        or message["output_transcription"]
        or message["citations"]
        or message["turn_complete"]
        or message["interrupted"]
        or message["error_code"]
        or message["error_message"]
    ):
        return message

    return None


def _extract_binary_audio_chunks(event: Any) -> list[tuple[str, bytes]]:
    if AUDIO_DOWNLINK_MODE != "binary":
        return []

    content = getattr(event, "content", None)
    if not content:
        return []

    chunks: list[tuple[str, bytes]] = []
    for part in content.parts:
        inline_data = getattr(part, "inline_data", None) or getattr(part, "inlineData", None)
        if not inline_data:
            continue
        mime_type = getattr(inline_data, "mime_type", None) or getattr(inline_data, "mimeType", None)
        data = getattr(inline_data, "data", None)
        if mime_type and mime_type.startswith("audio/pcm") and isinstance(data, bytes):
            chunks.append((mime_type, data))
    return chunks


def _message_contains_agent_output(message: dict[str, Any]) -> bool:
    output_transcription = message.get("output_transcription") or {}
    if output_transcription.get("text"):
        return True
    for part in message.get("parts") or []:
        part_type = str(part.get("type") or "")
        if part_type == "audio/pcm":
            return True
        if part_type == "text" and part.get("data"):
            return True
    return False


def _extract_message_output_text(message: dict[str, Any]) -> str:
    output_transcription = message.get("output_transcription") or {}
    text = str(output_transcription.get("text") or "").strip()
    if text:
        return text
    parts = message.get("parts") or []
    text_chunks: list[str] = []
    for part in parts:
        if str(part.get("type") or "") != "text":
            continue
        data = str(part.get("data") or "").strip()
        if data:
            text_chunks.append(data)
    return " ".join(text_chunks).strip()


_DOC_PATH_DETOUR_MARKERS = (
    "which part of the docs",
    "specific section",
    "look at first",
    "quickstart",
    "quickstarts",
    "prefer to explore",
    "guides or features",
)


def _should_suppress_navigation_detour_output(
    *,
    output_text: str,
    dialogue_state: TutorDialogueState,
    navigation_runtime: DocNavigationRuntime,
) -> bool:
    if not output_text:
        return False
    if dialogue_state.guided_use_case_mode:
        return False

    actively_navigating = navigation_runtime.is_running() or navigation_runtime.status in {
        "launching",
        "navigating",
        "locating_use_cases",
        "resumed",
        "pause_requested",
        "interrupted",
    }
    if not actively_navigating:
        return False

    normalized = normalize_text(output_text)
    return any(marker in normalized for marker in _DOC_PATH_DETOUR_MARKERS)


def _send_text_to_agent(live_request_queue: LiveRequestQueue, text: str) -> None:
    content = Content(role="user", parts=[Part.from_text(text=text)])
    live_request_queue.send_content(content=content)


def _local_agent_event(
    text: str,
    *,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "type": "agent_event",
        "author": "cloudtutor.flow",
        "is_partial": False,
        "turn_complete": True,
        "interrupted": False,
        "parts": [{"type": "text", "data": text}],
        "input_transcription": None,
        "output_transcription": {"text": text, "is_final": True},
        "reason": reason,
        "time_utc": utc_now_iso(),
    }
    if metadata:
        payload["flow_metadata"] = metadata
    return payload


async def _send_flow_message(
    websocket: WebSocket,
    *,
    text: str,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    await websocket.send_text(
        json.dumps(_local_agent_event(text, reason=reason, metadata=metadata))
    )


@dataclass
class DocNavigationRuntime:
    status: str = "idle"
    topic: str | None = None
    provider: ComputerUseProvider = field(default_factory=default_computer_use_provider)
    task: asyncio.Task[None] | None = None
    pause_requested: bool = False
    paused_summary: str | None = None
    paused_url: str | None = None
    live_session_url: str | None = None
    active_steps: list[dict[str, Any]] = field(default_factory=list)
    paused_steps: list[dict[str, Any]] = field(default_factory=list)
    pending_run_id: str | None = None
    pending_confirmation_id: str | None = None
    pending_confirmation_action: str | None = None
    pending_confirmation_step: int | None = None
    pending_confirmation_explanation: str | None = None
    last_control_text: str | None = None
    last_control_at_monotonic: float = 0.0
    narration_urgent_queue: list[str] = field(default_factory=list)
    narration_scan_pending: str | None = None
    last_narration_text: str | None = None
    last_narration_at_monotonic: float = 0.0
    last_scan_narration_text: str | None = None

    def is_running(self) -> bool:
        return self.task is not None and not self.task.done()


@dataclass
class BargeInRuntime:
    suppress_agent_output: bool = False
    last_client_interrupt_at_monotonic: float = 0.0
    agent_output_in_progress: bool = False
    suppress_started_at_monotonic: float = 0.0
    agent_turn_started: bool = False


def _clear_pending_doc_navigation_confirmation(
    navigation_runtime: DocNavigationRuntime,
) -> None:
    navigation_runtime.pending_run_id = None
    navigation_runtime.pending_confirmation_id = None
    navigation_runtime.pending_confirmation_action = None
    navigation_runtime.pending_confirmation_step = None
    navigation_runtime.pending_confirmation_explanation = None


def _clear_doc_progress_narration_state(
    navigation_runtime: DocNavigationRuntime,
) -> None:
    navigation_runtime.narration_urgent_queue.clear()
    navigation_runtime.narration_scan_pending = None
    navigation_runtime.last_narration_text = None
    navigation_runtime.last_narration_at_monotonic = 0.0
    navigation_runtime.last_scan_narration_text = None


def _set_pending_doc_navigation_confirmation(
    navigation_runtime: DocNavigationRuntime,
    *,
    run_id: str | None,
    pending_confirmation: Any | None,
) -> bool:
    _clear_pending_doc_navigation_confirmation(navigation_runtime)
    if not run_id or pending_confirmation is None:
        return False

    confirmation_id = str(
        getattr(pending_confirmation, "confirmation_id", "") or ""
    ).strip()
    action = str(getattr(pending_confirmation, "action", "") or "").strip()
    if not confirmation_id or not action:
        return False

    navigation_runtime.pending_run_id = run_id
    navigation_runtime.pending_confirmation_id = confirmation_id
    navigation_runtime.pending_confirmation_action = action
    step_index = getattr(pending_confirmation, "step_index", None)
    if isinstance(step_index, int):
        navigation_runtime.pending_confirmation_step = step_index
    else:
        try:
            navigation_runtime.pending_confirmation_step = int(str(step_index))
        except (TypeError, ValueError):
            navigation_runtime.pending_confirmation_step = None
    explanation = getattr(pending_confirmation, "explanation", None)
    if isinstance(explanation, str) and explanation.strip():
        navigation_runtime.pending_confirmation_explanation = explanation.strip()
    return True


def _has_pending_doc_navigation_confirmation(
    navigation_runtime: DocNavigationRuntime,
) -> bool:
    return bool(
        navigation_runtime.pending_run_id and navigation_runtime.pending_confirmation_id
    )


def _topic_to_docs_hint(topic: str) -> str:
    normalized = normalize_text(topic)
    if " vs " in f" {normalized} ":
        primary = normalized.split(" vs ", 1)[0].strip()
        if primary and primary != normalized:
            return _topic_to_docs_hint(primary)

    docs_map: tuple[tuple[tuple[str, ...], str], ...] = (
        (
            (
                "firebase cloud functions",
                "firebase cloud function",
                "firebase functions",
                "firebase function",
            ),
            "https://firebase.google.com/docs/functions",
        ),
        (
            ("firebase hosting",),
            "https://firebase.google.com/docs/hosting",
        ),
        (
            ("firestore",),
            "https://firebase.google.com/docs/firestore",
        ),
        (
            ("firebase",),
            "https://firebase.google.com/docs",
        ),
        (
            ("cloud functions", "cloud function"),
            "https://cloud.google.com/functions/docs",
        ),
        (
            ("cloud run",),
            "https://cloud.google.com/run/docs",
        ),
        (
            ("gke", "kubernetes engine"),
            "https://cloud.google.com/kubernetes-engine/docs",
        ),
        (
            ("bigquery",),
            "https://cloud.google.com/bigquery/docs",
        ),
        (
            ("cloud sql",),
            "https://cloud.google.com/sql/docs",
        ),
        (
            ("cloud storage", "gcs"),
            "https://cloud.google.com/storage/docs",
        ),
        (
            ("pub/sub", "pubsub"),
            "https://cloud.google.com/pubsub/docs",
        ),
        (
            ("vertex ai",),
            "https://cloud.google.com/vertex-ai/docs",
        ),
        (
            ("app engine",),
            "https://cloud.google.com/appengine/docs",
        ),
        (
            ("iam", "identity and access management"),
            "https://cloud.google.com/iam/docs",
        ),
        (
            ("vpc", "virtual private cloud"),
            "https://cloud.google.com/vpc/docs",
        ),
        (
            ("cloud build",),
            "https://cloud.google.com/build/docs",
        ),
        (
            ("cloud deploy",),
            "https://cloud.google.com/deploy/docs",
        ),
        (
            ("cloud armor",),
            "https://cloud.google.com/armor/docs",
        ),
        (
            ("cloud load balancing", "load balancing"),
            "https://cloud.google.com/load-balancing/docs",
        ),
        (
            ("secret manager",),
            "https://cloud.google.com/secret-manager/docs",
        ),
        (
            ("artifact registry",),
            "https://cloud.google.com/artifact-registry/docs",
        ),
        (
            ("cloud scheduler",),
            "https://cloud.google.com/scheduler/docs",
        ),
        (
            ("cloud tasks",),
            "https://cloud.google.com/tasks/docs",
        ),
        (
            ("cloud logging",),
            "https://cloud.google.com/logging/docs",
        ),
        (
            ("cloud monitoring", "stackdriver monitoring"),
            "https://cloud.google.com/monitoring/docs",
        ),
        (
            ("cloud dns",),
            "https://cloud.google.com/dns/docs",
        ),
        (
            ("cloud cdn",),
            "https://cloud.google.com/cdn/docs",
        ),
        (
            ("cloud nat",),
            "https://cloud.google.com/nat/docs",
        ),
        (
            ("spanner",),
            "https://cloud.google.com/spanner/docs",
        ),
        (
            ("dataflow",),
            "https://cloud.google.com/dataflow/docs",
        ),
        (
            ("dataproc",),
            "https://cloud.google.com/dataproc/docs",
        ),
        (
            ("memorystore",),
            "https://cloud.google.com/memorystore/docs",
        ),
        (
            ("cloud composer", "composer"),
            "https://cloud.google.com/composer/docs",
        ),
        (
            ("workflows",),
            "https://cloud.google.com/workflows/docs",
        ),
        (
            ("eventarc",),
            "https://cloud.google.com/eventarc/docs",
        ),
        (
            ("api gateway",),
            "https://cloud.google.com/api-gateway/docs",
        ),
        (
            ("service account", "service accounts"),
            "https://cloud.google.com/iam/docs/service-accounts",
        ),
    )

    for keywords, docs_url in docs_map:
        if any(keyword in normalized for keyword in keywords):
            return docs_url

    # Deterministic fallback: avoid free-form search queries that can drift
    # when topic extraction fails (for example, using conversational sentences).
    return "https://cloud.google.com/docs"

def _resolve_doc_topic(
    *,
    dialogue_state: TutorDialogueState,
    navigation_runtime: DocNavigationRuntime,
    fallback_text: str = "",
) -> str:
    # Prefer the currently discussed user topic first; only then fall back to
    # ad-hoc text and previous navigation state.
    current_topic = dialogue_state.current_topic or ""
    inferred_current = infer_cloud_service_topic(current_topic)
    if inferred_current:
        return inferred_current

    inferred_fallback = infer_cloud_service_topic(fallback_text or "")
    if inferred_fallback:
        return inferred_fallback

    inferred_nav_topic = infer_cloud_service_topic(navigation_runtime.topic or "")
    if inferred_nav_topic:
        return inferred_nav_topic

    inferred_last_answer = infer_cloud_service_topic(dialogue_state.last_answer or "")
    if inferred_last_answer:
        return inferred_last_answer

    # Do not fall back to arbitrary conversational text.
    # If service inference fails, keep a deterministic generic topic.
    return "Google Cloud service"


def _build_doc_navigation_query(topic: str) -> str:
    docs_hint = _topic_to_docs_hint(topic)
    cleaned_topic = re.sub(r"\s+", " ", topic.strip()) or "the requested cloud service"
    return (
        "Run a browser documentation walkthrough for the requested service.\n"
        f"Topic: {cleaned_topic}\n"
        f"First action: open this URL exactly: {docs_hint}\n"
        "Dismiss any cookie or consent banners (Reject all or Accept all).\n"
        "If the opened URL is a Google Search results page, click the first official Google Cloud or Firebase documentation link to navigate to the actual documentation page.\n"
        "Stop immediately after the main documentation page for the requested service has loaded and is visible.\n"
        "Do not navigate to deep sub-pages. Do not search for specific use-case sections."
    )


def _build_next_use_case_query(
    *,
    topic: str,
    current_index: int,
    current_url: str | None,
) -> str:
    docs_hint = current_url or _topic_to_docs_hint(topic)
    cleaned_topic = re.sub(r"\s+", " ", topic.strip()) or "the requested cloud service"
    target_index = max(1, current_index + 1)
    return (
        "Continue a browser documentation walkthrough for the requested service.\n"
        f"Topic: {cleaned_topic}\n"
        f"Start from this URL exactly: {docs_hint}\n"
        f"Current exploring index: {max(1, current_index)}\n"
        f"Target index: {target_index}\n"
        "Find the next distinct major section (e.g. Quickstart, Use Cases, Core Concepts) that is different from previously explained ones.\n"
        "Click on it, wait for it to load, and then stop."
    )


_USE_CASE_URL_MARKERS = (
    "use-case", "use_case", "use-cases", "use_cases",
    "when-to-use", "common-scenarios", "best-for", "overview",
)
_USE_CASE_TEXT_MARKERS = (
    "use case", "use cases", "when to use", "common scenario",
    "best for", "practical example", "real-world", "real world",
    "typical use", "popular use",
)


def _is_use_case_section_ready(
    *,
    visited_url: str | None,
    final_reasoning: str | None,
    steps: list[dict[str, Any]],
) -> bool:
    """Returns True if we reached a docs page (the interactive explorer just needs a URL)."""
    return bool(visited_url)


def _reset_guided_use_case_mode(dialogue_state: TutorDialogueState) -> None:
    dialogue_state.guided_use_case_mode = False
    dialogue_state.guided_use_case_ready = False
    dialogue_state.guided_use_case_index = 0
    dialogue_state.guided_use_case_topic = None
    dialogue_state.guided_use_case_url = None
    dialogue_state.guided_use_case_summary = None


def _enable_guided_use_case_mode(
    dialogue_state: TutorDialogueState,
    *,
    topic: str,
    visited_url: str | None,
    summary: str | None,
) -> None:
    dialogue_state.guided_use_case_mode = True
    dialogue_state.guided_use_case_ready = True
    dialogue_state.guided_use_case_index = 1
    dialogue_state.guided_use_case_topic = topic
    dialogue_state.guided_use_case_url = visited_url
    dialogue_state.guided_use_case_summary = (summary or "")[:5000] or None


def _build_guided_use_case_prompt(
    *,
    dialogue_state: TutorDialogueState,
    mode: str,
    user_text: str | None = None,
) -> str:
    topic = dialogue_state.guided_use_case_topic or dialogue_state.current_topic or "this service"
    index = max(1, dialogue_state.guided_use_case_index)
    visited_url = dialogue_state.guided_use_case_url or "unknown"
    summary = dialogue_state.guided_use_case_summary or "N/A"

    common_block = (
        "Flow update: we are in interactive documentation explorer mode.\n"
        f"Topic: {topic}\n"
        f"Visited URL: {visited_url}\n"
        f"Navigation summary: {summary}\n"
        "- Keep tone highly conversational and voice-first.\n"
        "- Base your explanations heavily on your internal knowledge of this topic, grounded by the fact that the docs are open.\n"
        "- If a diagram likely exists for what you are explaining, explicitly say: \"As you can see in the documentation...\" and briefly explain it.\n"
        "- Keep each response concise and interrupt-friendly.\n"
    )

    if mode == "start":
        topic_name = topic if topic and topic.strip() else "the requested service"
        return (
            f"{common_block}"
            "IMPORTANT: The official documentation has been successfully opened.\n"
            "You MUST start by saying exactly this (or something extremely close to it):\n"
            f'"I have the official documentation for {topic_name} open now. We can dive straight into exactly what it is, look at some common use cases, or explore pricing. What would you like to tackle first?"\n'
            "We want to let the user drive the exploration.\n"
            "Do NOT start explaining anything until they reply."
        )

    if mode == "next":
        return (
            f"{common_block}"
            f"The user asked to look at another section or continue. Current step index: {index}.\n"
            "Briefly mention that you've navigated to a new section of the docs.\n"
            "Introduce the new concept/use case clearly and concisely.\n"
            "End with: \"Shall we go deeper into this, or explore something else?\""
        )

    follow_up = user_text or ""
    return (
        f"{common_block}"
        f"Current use-case index: {index}\n"
        f"User follow-up: {follow_up}\n"
        "Answer their question about the current use case first.\n"
        "After answering, ask whether to continue to the next use case."
    )


def _should_send_doc_progress_update(
    *,
    action: str,
    scroll_counter: int,
) -> bool:
    if action in {"scroll_document", "scroll_at"}:
        return scroll_counter <= 1 or scroll_counter % 3 == 0
    return True


def _doc_progress_narration_enabled() -> bool:
    return False


def _doc_progress_narration_min_interval_seconds() -> float:
    raw = os.getenv("CLOUDTUTOR_DOC_PROGRESS_NARRATION_MIN_INTERVAL_MS", "2200")
    try:
        interval_ms = int(raw)
    except (TypeError, ValueError):
        interval_ms = 2200
    interval_ms = max(700, min(10_000, interval_ms))
    return interval_ms / 1000.0


def _classify_doc_progress_narration(progress: dict[str, Any]) -> str:
    event_type = str(progress.get("event") or "")
    action = str(progress.get("action") or "")
    status = str(progress.get("status") or "")
    has_url = bool(str(progress.get("url") or "").strip())

    if event_type in {"run_started", "safety_confirmation_required"}:
        return "urgent"
    if event_type == "model_thinking":
        return "scan"
    if event_type == "action_planned":
        if action in {"navigate", "search", "open_web_browser", "type_text_at"}:
            return "urgent"
        if action in {"scroll_document", "scroll_at", "hover_at", "click_at"}:
            return "scan"
        return "urgent"
    if event_type == "action_result":
        if status in {"error", "unsupported"}:
            return "urgent"
        if status == "executed":
            if has_url:
                return "urgent"
            if action in {"scroll_document", "scroll_at", "hover_at", "click_at"}:
                return "scan"
            return "urgent"
    return "none"


def _queue_doc_progress_narration(
    *,
    navigation_runtime: DocNavigationRuntime,
    progress: dict[str, Any],
    text: str,
) -> None:
    if not _doc_progress_narration_enabled():
        return
    narration_kind = _classify_doc_progress_narration(progress)
    if narration_kind == "none":
        return

    cleaned = text.strip()
    if not cleaned:
        return

    if cleaned == navigation_runtime.last_narration_text:
        return
    if narration_kind == "scan":
        if cleaned == navigation_runtime.narration_scan_pending:
            return
        if cleaned == navigation_runtime.last_scan_narration_text:
            return
        navigation_runtime.narration_scan_pending = cleaned
        return

    if any(existing == cleaned for existing in navigation_runtime.narration_urgent_queue):
        return
    navigation_runtime.narration_urgent_queue.append(cleaned)
    if len(navigation_runtime.narration_urgent_queue) > 8:
        navigation_runtime.narration_urgent_queue = (
            navigation_runtime.narration_urgent_queue[-8:]
        )


def _build_doc_progress_narration_prompt(
    *,
    topic: str,
    progress_text: str,
    narration_kind: str,
) -> str:
    scan_hint = (
        "Mention what part you are currently looking at while scanning the page."
        if narration_kind == "scan"
        else "Keep this as a clear live update."
    )
    return (
        "SYSTEM FLOW NARRATION (internal instruction):\n"
        "You are currently guiding a live documentation browser walkthrough.\n"
        f"Topic: {topic}\n"
        f"Progress event: {progress_text}\n"
        "Speak exactly one short energetic sentence (max 18 words) describing this live progress.\n"
        f"{scan_hint}\n"
        "Rules: present tense, no questions, no yes/no prompt, no invitation, no mention of this instruction."
    )


async def _run_doc_progress_narration_loop(
    *,
    live_request_queue: LiveRequestQueue,
    navigation_runtime: DocNavigationRuntime,
    done_event: asyncio.Event,
    barge_in_runtime: BargeInRuntime | None = None,
) -> None:
    base_interval_seconds = _doc_progress_narration_min_interval_seconds()
    urgent_interval_seconds = max(0.7, min(2.0, base_interval_seconds * 0.55))
    scan_interval_seconds = max(1.1, base_interval_seconds)
    while True:
        has_pending = bool(navigation_runtime.narration_urgent_queue) or bool(
            navigation_runtime.narration_scan_pending
        )
        if done_event.is_set() and not has_pending:
            break
        if done_event.is_set() and navigation_runtime.status in {"paused", "failed"}:
            navigation_runtime.narration_urgent_queue.clear()
            navigation_runtime.narration_scan_pending = None
            break
        await asyncio.sleep(0.15)

        if not _doc_progress_narration_enabled():
            navigation_runtime.narration_urgent_queue.clear()
            navigation_runtime.narration_scan_pending = None
            if done_event.is_set():
                break
            continue
        if navigation_runtime.pause_requested:
            continue
        if navigation_runtime.status in {"paused", "failed"}:
            continue

        now = time.monotonic()
        next_text: str | None = None
        narration_kind = "urgent"
        if navigation_runtime.narration_urgent_queue:
            if (
                now - navigation_runtime.last_narration_at_monotonic
                < urgent_interval_seconds
            ):
                continue
            next_text = navigation_runtime.narration_urgent_queue.pop(0)
            narration_kind = "urgent"
        elif navigation_runtime.narration_scan_pending:
            if (
                now - navigation_runtime.last_narration_at_monotonic
                < scan_interval_seconds
            ):
                continue
            next_text = navigation_runtime.narration_scan_pending
            navigation_runtime.narration_scan_pending = None
            navigation_runtime.last_scan_narration_text = next_text
            narration_kind = "scan"

        if not next_text:
            continue

        if (
            barge_in_runtime is not None
            and barge_in_runtime.suppress_agent_output
            and not barge_in_runtime.agent_output_in_progress
            and navigation_runtime.status in {"launching", "navigating"}
            and now - barge_in_runtime.suppress_started_at_monotonic > 0.55
        ):
            barge_in_runtime.suppress_agent_output = False

        navigation_runtime.last_narration_text = next_text
        navigation_runtime.last_narration_at_monotonic = now
        topic = navigation_runtime.topic or "this cloud service"
        _send_text_to_agent(
            live_request_queue,
            _build_doc_progress_narration_prompt(
                topic=topic,
                progress_text=next_text,
                narration_kind=narration_kind,
            ),
        )


def _describe_doc_progress_event(event: dict[str, Any]) -> str | None:
    event_type = str(event.get("event") or "")
    action = str(event.get("action") or "")
    status = str(event.get("status") or "")
    url = str(event.get("url") or "").strip()

    if event_type == "run_started":
        return "I started the live documentation walkthrough and I’m opening the browser session."
    if event_type == "model_thinking":
        return "I’m reading the page structure now and looking for sections that explain practical use cases."
    if event_type == "action_planned":
        if action == "navigate":
            return "I’m opening the official documentation page now."
        if action == "search":
            return "I’m running a focused docs search to find practical use cases."
        if action in {"click_at", "hover_at"}:
            return "I found a promising section and I’m opening it now."
        if action in {"scroll_document", "scroll_at"}:
            return "I’m scrolling this section and checking headings plus examples to pinpoint concrete use cases."
        if action == "type_text_at":
            return "I’m entering a targeted query to jump straight to use cases."
        if action == "open_web_browser":
            return "I’m initializing the browser for the walkthrough."
        return "I’m taking the next browser step for your walkthrough."
    if event_type == "action_result":
        if status == "executed":
            if action in {"scroll_document", "scroll_at", "click_at", "hover_at"}:
                if url:
                    return f"I’m now looking at this section: {url}. I’m extracting the next concrete use case."
                return "I’m now reviewing this section and extracting the next concrete use case."
            if url:
                return f"I completed that step. We are now on {url}."
            return "I completed that step and I’m moving to the next relevant section."
        if status in {"error", "unsupported"}:
            return "That browser step failed, so I’m trying an alternative way to continue."
    if event_type == "safety_confirmation_required":
        return "I reached a safety-gated browser action and I need your approval to continue."
    return None


async def _relay_doc_navigation_progress(
    *,
    websocket: WebSocket,
    navigation_runtime: DocNavigationRuntime,
    progress_queue: asyncio.Queue[dict[str, Any]],
    done_event: asyncio.Event,
) -> None:
    scroll_counter = 0
    while True:
        if done_event.is_set() and progress_queue.empty():
            break
        try:
            progress = await asyncio.wait_for(progress_queue.get(), timeout=0.25)
        except TimeoutError:
            continue

        event_type = str(progress.get("event") or "")
        action = str(progress.get("action") or "")
        step_index_raw = progress.get("step_index")
        step_index = step_index_raw if isinstance(step_index_raw, int) else None
        status = str(progress.get("status") or "")
        url = str(progress.get("url") or "").strip() or None
        error = str(progress.get("error") or "").strip() or None

        if event_type == "action_result":
            if action in {"scroll_document", "scroll_at"}:
                scroll_counter += 1

            if step_index is None:
                step_index = len(navigation_runtime.active_steps) + 1
            navigation_runtime.active_steps.append(
                {
                    "index": step_index,
                    "action": action or "step",
                    "status": status or "unknown",
                    "url": url,
                    "error": error,
                }
            )
            navigation_runtime.active_steps = navigation_runtime.active_steps[-20:]
            if url:
                navigation_runtime.paused_url = url

        text = _describe_doc_progress_event(progress)
        if not text:
            continue
        if action in {"scroll_document", "scroll_at"} and not _should_send_doc_progress_update(
            action=action,
            scroll_counter=scroll_counter,
        ):
            continue

        await _send_flow_message(
            websocket,
            text=text,
            reason="doc_navigation_progress",
            metadata={
                "topic": navigation_runtime.topic,
                "session_url": navigation_runtime.live_session_url,
                "visited_url": navigation_runtime.paused_url,
                "active_steps": navigation_runtime.active_steps,
                "latest_event": text,
                "progress": progress,
            },
        )
        _queue_doc_progress_narration(
            navigation_runtime=navigation_runtime,
            progress=progress,
            text=text,
        )


def _agent_requests_doc_confirmation(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    offer_markers = (
        "show me more",
        "official docs",
        "official documentation",
        "open the docs",
        "open official docs",
        "in your browser",
        "would you like to know more",
        "want to know more",
        "should i continue",
        "reply yes or no",
    )
    return any(marker in normalized for marker in offer_markers)


def _agent_indicates_doc_navigation_started(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    started_markers = (
        "i'm launching your browser",
        "i am launching your browser",
        "pulling up the official docs",
        "i've opened the official documentation",
        "i have opened the official documentation",
        "continuing the browser walkthrough",
    )
    return any(marker in normalized for marker in started_markers)


def _normalize_control_text(text: str) -> str:
    lowered = text.strip().lower()
    collapsed = re.sub(r"[^\w\s']", " ", lowered)
    return " ".join(collapsed.split())


def _mark_control_text_seen(
    navigation_runtime: DocNavigationRuntime, normalized_control_text: str
) -> None:
    navigation_runtime.last_control_text = normalized_control_text
    navigation_runtime.last_control_at_monotonic = time.monotonic()


def _was_recent_control_text(
    navigation_runtime: DocNavigationRuntime,
    normalized_control_text: str,
    *,
    window_seconds: float = 3.0,
) -> bool:
    if not normalized_control_text:
        return False
    if navigation_runtime.last_control_text != normalized_control_text:
        return False
    elapsed = time.monotonic() - navigation_runtime.last_control_at_monotonic
    return elapsed <= window_seconds


def _should_route_voice_control_text(
    *,
    text_data: str,
    dialogue_state: TutorDialogueState,
    navigation_runtime: DocNavigationRuntime,
) -> bool:
    normalized = normalize_text(text_data)
    if not normalized:
        return False

    if _has_pending_doc_navigation_confirmation(navigation_runtime):
        return True
    if dialogue_state.awaiting_doc_confirmation:
        return True
    if dialogue_state.guided_use_case_mode:
        return True
    if navigation_runtime.status == "paused" and is_resume_request(normalized):
        return True
    if is_more_details_request(normalized):
        return True
    return False


def _best_step_url(steps: list[ComputerUseStep]) -> str | None:
    candidate: str | None = None
    for step in steps:
        if step.url:
            candidate = step.url
            if "cloud.google.com" in step.url or "firebase.google.com" in step.url:
                return step.url
    return candidate


def _steps_payload(steps: list[ComputerUseStep], limit: int = 12) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for step in steps[:limit]:
        payload.append(
            {
                "index": step.index,
                "action": step.action,
                "status": step.status,
                "url": step.url,
                "error": step.error,
            }
        )
    return payload


def _build_stateful_prompt(
    user_text: str,
    dialogue_state: TutorDialogueState,
    *,
    require_grounding: bool,
) -> str:
    state = dialogue_state.snapshot()
    state_block = json.dumps(state, ensure_ascii=True)
    grounding_policy = (
        "- This query is likely time-sensitive or factual. Use google_search before finalizing.\n"
        "- Include a final section exactly titled: Sources:\n"
        "- Under Sources, provide 2-4 markdown links.\n"
        "- If you cannot verify fresh sources, explicitly say so and give a cautious best-effort answer."
        if require_grounding
        else "- Add a short Sources section with links when external facts are used."
    )
    return (
        "Tutor flow context (internal):\n"
        f"{state_block}\n"
        "Response policy:\n"
        "- Answer clearly in 2-5 short sentences.\n"
        "- If the user names a specific cloud product, answer that exact product instead of broad platform definitions.\n"
        '- End with this exact style of follow-up after explaining a cloud topic: '
        '"If you\'d like, I can show you the official docs in your browser. Just say show me more."\n'
        "- If the user asks for more details in a future turn, require explicit yes/no confirmation before starting doc navigation.\n"
        "- Do not expose internal reasoning or tool-step narration.\n"
        f"{grounding_policy}\n"
        f"User message:\n{user_text}"
    )


async def _run_doc_navigation_flow(
    *,
    websocket: WebSocket,
    live_request_queue: LiveRequestQueue,
    dialogue_state: TutorDialogueState,
    navigation_runtime: DocNavigationRuntime,
    barge_in_runtime: BargeInRuntime | None = None,
) -> None:
    topic = _resolve_doc_topic(
        dialogue_state=dialogue_state,
        navigation_runtime=navigation_runtime,
    )
    navigation_runtime.topic = topic
    provider = navigation_runtime.provider
    docs_target_url = _topic_to_docs_hint(topic)
    navigation_runtime.status = "launching"
    navigation_runtime.pause_requested = False
    navigation_runtime.paused_summary = None
    navigation_runtime.paused_url = None
    navigation_runtime.live_session_url = None
    navigation_runtime.active_steps = []
    navigation_runtime.paused_steps = []
    _clear_doc_progress_narration_state(navigation_runtime)
    _clear_pending_doc_navigation_confirmation(navigation_runtime)
    _reset_guided_use_case_mode(dialogue_state)

    doc_nav_max_steps = max(
        1, min(30, int(os.getenv("CLOUDTUTOR_DOC_NAV_MAX_STEPS", "12")))
    )
    request = ComputerUseRunRequest(
        query=_build_doc_navigation_query(topic),
        initial_url=docs_target_url,
        max_steps=doc_nav_max_steps,
        provider=provider,
    )

    progress_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
    progress_done = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _progress_callback(progress: dict[str, Any]) -> None:
        def _enqueue() -> None:
            if progress_queue.full():
                try:
                    progress_queue.get_nowait()
                except Exception:  # noqa: BLE001
                    pass
            try:
                progress_queue.put_nowait(progress)
            except Exception:  # noqa: BLE001
                pass

        loop.call_soon_threadsafe(_enqueue)

    progress_task: asyncio.Task[None] | None = None
    narration_task: asyncio.Task[None] | None = None
    try:
        session = await asyncio.to_thread(
            _create_computer_use_session,
            request,
            _progress_callback,
        )
    except (ComputerUseDependencyError, ComputerUseConfigurationError) as exc:
        navigation_runtime.status = "failed"
        await _send_flow_message(
            websocket,
            text=(
                "I could not launch the browser session right now, so I’ll switch to "
                "grounded search and still walk you through the basics."
            ),
            reason="doc_navigation_failed_fallback",
            metadata={"topic": topic, "provider": provider, "error": str(exc)},
        )
        dialogue_state.branch_context = "doc_navigation_fallback"
        _send_text_to_agent(
            live_request_queue,
            (
                "Flow update: browser session launch failed.\n"
                f"Topic: {topic}\n"
                f"Provider: {provider}\n"
                f"Error: {exc}\n"
                "Use google_search to provide a concise beginner-friendly walkthrough with sources."
            ),
        )
        return
    except Exception as exc:  # noqa: BLE001
        navigation_runtime.status = "failed"
        await _send_flow_message(
            websocket,
            text=(
                "I hit an unexpected issue while launching the browser session. "
                "I’ll continue with grounded search so you still get a reliable deep-dive."
            ),
            reason="doc_navigation_exception_fallback",
            metadata={"topic": topic, "provider": provider, "error": str(exc)},
        )
        dialogue_state.branch_context = "doc_navigation_fallback"
        _send_text_to_agent(
            live_request_queue,
            (
                "Flow update: browser session launch raised an exception.\n"
                f"Topic: {topic}\n"
                f"Provider: {provider}\n"
                f"Error: {exc}\n"
                "Use google_search and provide a concise deep explanation with sources."
            ),
        )
        return

    navigation_runtime.live_session_url = getattr(session.backend, "debug_url", None)
    progress_task = asyncio.create_task(
        _relay_doc_navigation_progress(
            websocket=websocket,
            navigation_runtime=navigation_runtime,
            progress_queue=progress_queue,
            done_event=progress_done,
        )
    )
    narration_task = asyncio.create_task(
        _run_doc_progress_narration_loop(
            live_request_queue=live_request_queue,
            navigation_runtime=navigation_runtime,
            done_event=progress_done,
            barge_in_runtime=barge_in_runtime,
        )
    )

    await _send_flow_message(
        websocket,
        text=(
            f"Great, I’m launching your browser for {topic} now. "
            f"I’m opening {docs_target_url} first and then moving to core use cases."
        ),
        reason="doc_navigation_launching",
        metadata={
            "topic": topic,
            "provider": provider,
            "session_url": navigation_runtime.live_session_url,
            "target_url": docs_target_url,
        },
    )

    await _send_flow_message(
        websocket,
        text=(
            "I’m now navigating to the most relevant documentation section and then to practical use cases."
        ),
        reason="doc_navigation_searching",
        metadata={
            "topic": topic,
            "provider": provider,
            "session_url": navigation_runtime.live_session_url,
            "target_url": docs_target_url,
        },
    )
    navigation_runtime.status = "navigating"

    try:
        result = await asyncio.to_thread(
            _execute_computer_use_session,
            session=session,
            request=request,
            keep_session_open=True,
        )
    except HTTPException as exc:
        navigation_runtime.status = "failed"
        await _send_flow_message(
            websocket,
            text=(
                "I could not complete browser navigation right now, so I’ll switch to "
                "grounded search and still walk you through the basics."
            ),
            reason="doc_navigation_failed_fallback",
            metadata={
                "topic": topic,
                "provider": provider,
                "session_url": navigation_runtime.live_session_url,
                "error": str(exc.detail),
            },
        )
        dialogue_state.branch_context = "doc_navigation_fallback"
        _send_text_to_agent(
            live_request_queue,
            (
                "Flow update: browser navigation failed.\n"
                f"Topic: {topic}\n"
                f"Error: {exc.detail}\n"
                "Use google_search to provide a concise beginner-friendly walkthrough with sources."
            ),
        )
        progress_done.set()
        if progress_task is not None:
            await progress_task
        return
    except Exception as exc:  # noqa: BLE001
        navigation_runtime.status = "failed"
        await _send_flow_message(
            websocket,
            text=(
                "I hit an unexpected issue while navigating docs. I’ll continue with "
                "grounded search so you still get a reliable deep-dive."
            ),
            reason="doc_navigation_exception_fallback",
            metadata={
                "topic": topic,
                "provider": provider,
                "session_url": navigation_runtime.live_session_url,
                "error": str(exc),
            },
        )
        dialogue_state.branch_context = "doc_navigation_fallback"
        _send_text_to_agent(
            live_request_queue,
            (
                "Flow update: browser navigation raised an exception.\n"
                f"Topic: {topic}\n"
                f"Error: {exc}\n"
                "Use google_search and provide a concise deep explanation with sources."
            ),
        )
        progress_done.set()
        if progress_task is not None:
            await progress_task
        return
    finally:
        progress_done.set()
        if progress_task is not None:
            try:
                await progress_task
            except Exception:  # noqa: BLE001
                pass
        if narration_task is not None:
            try:
                await narration_task
            except Exception:  # noqa: BLE001
                pass

    visited_url = _best_step_url(result.steps) or result.debug_url
    steps_payload = _steps_payload(result.steps)
    if steps_payload:
        navigation_runtime.active_steps = steps_payload

    if navigation_runtime.pause_requested:
        navigation_runtime.status = "paused"
        navigation_runtime.paused_url = visited_url
        navigation_runtime.paused_steps = steps_payload
        navigation_runtime.paused_summary = result.final_reasoning or ""
        pause_text = (
            "I paused the doc walkthrough for your interruption. "
            "Say continue whenever you want me to resume from here."
        )
        pause_metadata: dict[str, Any] = {
            "topic": topic,
            "visited_url": visited_url,
            "session_url": navigation_runtime.live_session_url,
            "steps": steps_payload,
            "result_status": result.status,
        }
        if result.status == "awaiting_confirmation":
            _set_pending_doc_navigation_confirmation(
                navigation_runtime,
                run_id=result.run_id,
                pending_confirmation=result.pending_confirmation,
            )
            pause_text = (
                "I paused the doc walkthrough for your interruption at a safety-gated step. "
                "Say yes when you want me to continue browser actions, or no to stop."
            )
            pause_metadata["run_id"] = result.run_id
            pause_metadata["pending_confirmation"] = (
                result.pending_confirmation.model_dump()
                if result.pending_confirmation
                else None
            )
        await _send_flow_message(
            websocket,
            text=pause_text,
            reason="doc_navigation_paused",
            metadata=pause_metadata,
        )
        return

    if result.status == "awaiting_confirmation":
        navigation_runtime.status = "paused"
        navigation_runtime.paused_summary = result.error or ""
        navigation_runtime.paused_url = visited_url
        navigation_runtime.paused_steps = steps_payload
        _set_pending_doc_navigation_confirmation(
            navigation_runtime,
            run_id=result.run_id,
            pending_confirmation=result.pending_confirmation,
        )
        await _send_flow_message(
            websocket,
            text=(
                "I reached a safety-gated browser action and paused automatically. "
                "Say yes to continue, or no to stop browser actions."
            ),
            reason="doc_navigation_safety_pause",
            metadata={
                "topic": topic,
                "visited_url": visited_url,
                "session_url": navigation_runtime.live_session_url,
                "steps": steps_payload,
                "run_id": result.run_id,
                "pending_confirmation": (
                    result.pending_confirmation.model_dump()
                    if result.pending_confirmation
                    else None
                ),
            },
        )
        return

    if result.status in {"failed", "safety_denied"}:
        navigation_runtime.status = "failed"
        _clear_pending_doc_navigation_confirmation(navigation_runtime)
        await _send_flow_message(
            websocket,
            text=(
                "I couldn’t finish browser navigation cleanly, so I’ll continue with "
                "grounded search to keep you moving."
            ),
            reason="doc_navigation_failed_status_fallback",
            metadata={
                "topic": topic,
                "visited_url": visited_url,
                "session_url": navigation_runtime.live_session_url,
                "status": result.status,
                "error": result.error,
            },
        )
        dialogue_state.branch_context = "doc_navigation_fallback"
        _reset_guided_use_case_mode(dialogue_state)
        _send_text_to_agent(
            live_request_queue,
            (
                "Flow update: browser navigation ended with a non-success status.\n"
                f"Topic: {topic}\n"
                f"Status: {result.status}\n"
                f"Error: {result.error or 'N/A'}\n"
                "Use google_search and provide a concise deeper explanation with sources."
            ),
        )
        return

    navigation_runtime.status = "completed"
    _clear_pending_doc_navigation_confirmation(navigation_runtime)

    # ── Readiness gate: only teach use cases if evidence confirms section was found ──
    use_case_ready = _is_use_case_section_ready(
        visited_url=visited_url,
        final_reasoning=result.final_reasoning,
        steps=steps_payload,
    )

    await _send_flow_message(
        websocket,
        text=(
            "I've checked the documentation page for use-case content."
            if not use_case_ready
            else (
                "I found the use-case section in the docs."
                + (f" Current page: {visited_url}" if visited_url else "")
            )
        ),
        reason="doc_navigation_locating_use_cases" if not use_case_ready else "doc_use_case_ready",
        metadata={
            "topic": topic,
            "visited_url": visited_url,
            "session_url": navigation_runtime.live_session_url,
            "steps": steps_payload,
            "use_case_ready": use_case_ready,
        },
    )

    if use_case_ready:
        _enable_guided_use_case_mode(
            dialogue_state,
            topic=topic,
            visited_url=visited_url,
            summary=result.final_reasoning,
        )
        dialogue_state.branch_context = "doc_navigation_explaining"
        _send_text_to_agent(
            live_request_queue,
            _build_guided_use_case_prompt(
                dialogue_state=dialogue_state,
                mode="start",
            ),
        )
    else:
        # Fallback: grounded explanation without pretending use cases were found
        dialogue_state.branch_context = "doc_navigation_fallback_no_use_cases"
        await _send_flow_message(
            websocket,
            text=(
                "I navigated the docs but couldn't locate a dedicated use-case section. "
                "I'll explain what I found using grounded sources instead."
            ),
            reason="doc_navigation_completed",
            metadata={
                "topic": topic,
                "visited_url": visited_url,
                "use_case_ready": False,
            },
        )
        _send_text_to_agent(
            live_request_queue,
            (
                f"Flow update: I navigated the docs for {topic} but could not locate "
                f"a dedicated use-case section.\n"
                f"Visited URL: {visited_url or 'unknown'}\n"
                f"Navigation summary: {result.final_reasoning or 'N/A'}\n"
                "Explain the topic concisely based on what you found, "
                "and use google_search to provide a grounded overview with sources."
            ),
        )


async def _run_doc_use_case_advance_flow(
    *,
    websocket: WebSocket,
    live_request_queue: LiveRequestQueue,
    dialogue_state: TutorDialogueState,
    navigation_runtime: DocNavigationRuntime,
    current_index: int,
    barge_in_runtime: BargeInRuntime | None = None,
) -> None:
    topic = (
        dialogue_state.guided_use_case_topic
        or navigation_runtime.topic
        or dialogue_state.current_topic
        or "this cloud service"
    )
    current_url = dialogue_state.guided_use_case_url or navigation_runtime.paused_url
    target_index = max(1, current_index + 1)
    provider = navigation_runtime.provider

    navigation_runtime.topic = topic
    navigation_runtime.status = "launching"
    navigation_runtime.pause_requested = False
    navigation_runtime.paused_summary = None
    navigation_runtime.paused_url = None
    navigation_runtime.active_steps = []
    navigation_runtime.paused_steps = []
    _clear_doc_progress_narration_state(navigation_runtime)
    _clear_pending_doc_navigation_confirmation(navigation_runtime)

    max_steps = max(1, min(30, int(os.getenv("CLOUDTUTOR_DOC_NAV_NEXT_MAX_STEPS", "10"))))
    request = ComputerUseRunRequest(
        query=_build_next_use_case_query(
            topic=topic,
            current_index=current_index,
            current_url=current_url,
        ),
        initial_url=current_url or _topic_to_docs_hint(topic),
        max_steps=max_steps,
        provider=provider,
    )

    progress_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
    progress_done = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _progress_callback(progress: dict[str, Any]) -> None:
        def _enqueue() -> None:
            if progress_queue.full():
                try:
                    progress_queue.get_nowait()
                except Exception:  # noqa: BLE001
                    pass
            try:
                progress_queue.put_nowait(progress)
            except Exception:  # noqa: BLE001
                pass

        loop.call_soon_threadsafe(_enqueue)

    progress_task: asyncio.Task[None] | None = None
    narration_task: asyncio.Task[None] | None = None
    try:
        session = await asyncio.to_thread(
            _create_computer_use_session,
            request,
            _progress_callback,
        )
    except (ComputerUseDependencyError, ComputerUseConfigurationError) as exc:
        navigation_runtime.status = "failed"
        await _send_flow_message(
            websocket,
            text=(
                "I could not move to the next use case in the browser right now. "
                "I can still answer your question here while we stay on this section."
            ),
            reason="doc_use_case_advance_failed",
            metadata={"topic": topic, "provider": provider, "error": str(exc)},
        )
        _send_text_to_agent(
            live_request_queue,
            (
                "Flow update: advancing to the next docs use case failed to launch.\n"
                f"Topic: {topic}\n"
                f"Error: {exc}\n"
                "Briefly explain that we remain on the current use case and ask whether to retry."
            ),
        )
        return
    except Exception as exc:  # noqa: BLE001
        navigation_runtime.status = "failed"
        await _send_flow_message(
            websocket,
            text=(
                "I hit an issue while moving to the next use case in the docs. "
                "I can keep teaching from the current section."
            ),
            reason="doc_use_case_advance_failed",
            metadata={"topic": topic, "provider": provider, "error": str(exc)},
        )
        _send_text_to_agent(
            live_request_queue,
            (
                "Flow update: advancing to the next docs use case raised an exception.\n"
                f"Topic: {topic}\n"
                f"Error: {exc}\n"
                "Briefly explain that we remain on the current use case and ask whether to retry."
            ),
        )
        return

    navigation_runtime.live_session_url = getattr(session.backend, "debug_url", None)
    progress_task = asyncio.create_task(
        _relay_doc_navigation_progress(
            websocket=websocket,
            navigation_runtime=navigation_runtime,
            progress_queue=progress_queue,
            done_event=progress_done,
        )
    )
    narration_task = asyncio.create_task(
        _run_doc_progress_narration_loop(
            live_request_queue=live_request_queue,
            navigation_runtime=navigation_runtime,
            done_event=progress_done,
            barge_in_runtime=barge_in_runtime,
        )
    )

    await _send_flow_message(
        websocket,
        text=(
            f"I’m moving to use case {target_index} now and I’ll stop there so we can discuss it."
        ),
        reason="doc_use_case_advancing",
        metadata={
            "topic": topic,
            "index": target_index,
            "provider": provider,
            "session_url": navigation_runtime.live_session_url,
            "start_url": request.initial_url,
        },
    )
    navigation_runtime.status = "navigating"

    try:
        result = await asyncio.to_thread(
            _execute_computer_use_session,
            session=session,
            request=request,
            keep_session_open=True,
        )
    except HTTPException as exc:
        navigation_runtime.status = "failed"
        await _send_flow_message(
            websocket,
            text=(
                "I couldn’t complete the move to the next use case right now. "
                "We can continue discussing the current one."
            ),
            reason="doc_use_case_advance_failed",
            metadata={
                "topic": topic,
                "provider": provider,
                "session_url": navigation_runtime.live_session_url,
                "error": str(exc.detail),
            },
        )
        _send_text_to_agent(
            live_request_queue,
            (
                "Flow update: advancing to the next docs use case failed.\n"
                f"Topic: {topic}\n"
                f"Error: {exc.detail}\n"
                "Keep teaching from the current use case and ask whether to retry."
            ),
        )
        progress_done.set()
        if progress_task is not None:
            await progress_task
        return
    except Exception as exc:  # noqa: BLE001
        navigation_runtime.status = "failed"
        await _send_flow_message(
            websocket,
            text=(
                "I hit an unexpected issue while moving to the next use case. "
                "Let’s stay on the current section for now."
            ),
            reason="doc_use_case_advance_failed",
            metadata={
                "topic": topic,
                "provider": provider,
                "session_url": navigation_runtime.live_session_url,
                "error": str(exc),
            },
        )
        _send_text_to_agent(
            live_request_queue,
            (
                "Flow update: advancing to the next docs use case raised an exception.\n"
                f"Topic: {topic}\n"
                f"Error: {exc}\n"
                "Keep teaching from the current use case and ask whether to retry."
            ),
        )
        progress_done.set()
        if progress_task is not None:
            await progress_task
        return
    finally:
        progress_done.set()
        if progress_task is not None:
            try:
                await progress_task
            except Exception:  # noqa: BLE001
                pass
        if narration_task is not None:
            try:
                await narration_task
            except Exception:  # noqa: BLE001
                pass

    visited_url = _best_step_url(result.steps) or result.debug_url
    steps_payload = _steps_payload(result.steps)
    if steps_payload:
        navigation_runtime.active_steps = steps_payload

    if navigation_runtime.pause_requested:
        navigation_runtime.status = "paused"
        navigation_runtime.paused_url = visited_url
        navigation_runtime.paused_steps = steps_payload
        navigation_runtime.paused_summary = result.final_reasoning or ""
        await _send_flow_message(
            websocket,
            text=(
                "I paused while moving to the next use case. "
                "Say continue whenever you want me to resume."
            ),
            reason="doc_navigation_paused",
            metadata={
                "topic": topic,
                "visited_url": visited_url,
                "session_url": navigation_runtime.live_session_url,
                "steps": steps_payload,
                "result_status": result.status,
            },
        )
        return

    if result.status == "awaiting_confirmation":
        navigation_runtime.status = "paused"
        navigation_runtime.paused_summary = result.error or ""
        navigation_runtime.paused_url = visited_url
        navigation_runtime.paused_steps = steps_payload
        _set_pending_doc_navigation_confirmation(
            navigation_runtime,
            run_id=result.run_id,
            pending_confirmation=result.pending_confirmation,
        )
        await _send_flow_message(
            websocket,
            text=(
                "I reached a safety-gated browser action while moving to the next use case. "
                "Say yes to continue, or no to stop."
            ),
            reason="doc_navigation_safety_pause",
            metadata={
                "topic": topic,
                "visited_url": visited_url,
                "session_url": navigation_runtime.live_session_url,
                "steps": steps_payload,
                "run_id": result.run_id,
                "pending_confirmation": (
                    result.pending_confirmation.model_dump()
                    if result.pending_confirmation
                    else None
                ),
            },
        )
        return

    if result.status in {"failed", "safety_denied"}:
        navigation_runtime.status = "failed"
        _clear_pending_doc_navigation_confirmation(navigation_runtime)
        await _send_flow_message(
            websocket,
            text=(
                "I couldn’t move to a distinct next use case yet. "
                "I can still keep teaching from the current one."
            ),
            reason="doc_use_case_advance_failed",
            metadata={
                "topic": topic,
                "visited_url": visited_url,
                "session_url": navigation_runtime.live_session_url,
                "status": result.status,
                "error": result.error,
            },
        )
        _send_text_to_agent(
            live_request_queue,
            (
                "Flow update: advancing to the next docs use case ended with a non-success status.\n"
                f"Topic: {topic}\n"
                f"Status: {result.status}\n"
                f"Error: {result.error or 'N/A'}\n"
                "Continue helping from the current use case and ask whether to retry."
            ),
        )
        return

    navigation_runtime.status = "completed"
    _clear_pending_doc_navigation_confirmation(navigation_runtime)

    use_case_ready = _is_use_case_section_ready(
        visited_url=visited_url,
        final_reasoning=result.final_reasoning,
        steps=steps_payload,
    )
    if not use_case_ready:
        await _send_flow_message(
            websocket,
            text=(
                "I could not confirm a distinct next use-case section yet. "
                "I can retry or keep discussing the current one."
            ),
            reason="doc_use_case_not_found",
            metadata={
                "topic": topic,
                "index": target_index,
                "visited_url": visited_url,
                "session_url": navigation_runtime.live_session_url,
                "steps": steps_payload,
            },
        )
        _send_text_to_agent(
            live_request_queue,
            (
                "Flow update: browser navigation did not confirm a distinct next use case yet.\n"
                f"Topic: {topic}\n"
                f"Target index: {target_index}\n"
                f"Visited URL: {visited_url or 'unknown'}\n"
                "Tell the user briefly and ask whether to retry navigation or ask a question now."
            ),
        )
        return

    dialogue_state.guided_use_case_mode = True
    dialogue_state.guided_use_case_ready = True
    dialogue_state.guided_use_case_index = target_index
    dialogue_state.guided_use_case_topic = topic
    dialogue_state.guided_use_case_url = visited_url
    dialogue_state.guided_use_case_summary = (result.final_reasoning or "")[:5000] or None
    dialogue_state.branch_context = "doc_use_case_next"

    await _send_flow_message(
        websocket,
        text=(
            f"I reached use case {target_index} in the docs. I’ll explain this one now, then pause for your questions."
        ),
        reason="doc_use_case_ready",
        metadata={
            "topic": topic,
            "index": target_index,
            "visited_url": visited_url,
            "session_url": navigation_runtime.live_session_url,
            "steps": steps_payload,
        },
    )
    _send_text_to_agent(
        live_request_queue,
        _build_guided_use_case_prompt(
            dialogue_state=dialogue_state,
            mode="next",
        ),
    )


async def _resume_doc_navigation_after_safety_confirmation(
    *,
    websocket: WebSocket,
    live_request_queue: LiveRequestQueue,
    dialogue_state: TutorDialogueState,
    navigation_runtime: DocNavigationRuntime,
    acknowledged: bool,
) -> None:
    topic = navigation_runtime.topic or dialogue_state.current_topic or "this topic"
    run_id = navigation_runtime.pending_run_id or ""
    confirmation_id = navigation_runtime.pending_confirmation_id or ""
    if not run_id or not confirmation_id:
        _clear_pending_doc_navigation_confirmation(navigation_runtime)
        await _send_flow_message(
            websocket,
            text=(
                "I no longer have a pending browser confirmation. "
                "If you want, say show me more and I will relaunch documentation navigation."
            ),
            reason="doc_navigation_safety_missing",
            metadata={"topic": topic, "session_url": navigation_runtime.live_session_url},
        )
        return

    if acknowledged:
        await _send_flow_message(
            websocket,
            text="Thanks. I’m continuing the browser walkthrough now.",
            reason="doc_navigation_safety_approved",
            metadata={"topic": topic, "session_url": navigation_runtime.live_session_url},
        )
    else:
        await _send_flow_message(
            websocket,
            text=(
                "Understood. I’ll stop browser actions here and continue teaching from this "
                "conversation context."
            ),
            reason="doc_navigation_safety_denied",
            metadata={"topic": topic, "session_url": navigation_runtime.live_session_url},
        )

    try:
        result = await asyncio.to_thread(
            computer_use_safety_response,
            ComputerUseSafetyResponseRequest(
                run_id=run_id,
                confirmation_id=confirmation_id,
                acknowledged=acknowledged,
                keep_session_open=True,
            ),
        )
    except HTTPException as exc:
        navigation_runtime.status = "paused"
        await _send_flow_message(
            websocket,
            text=(
                "I couldn’t process that safety reply yet. "
                "Please say yes to continue browser actions, or no to stop."
            ),
            reason="doc_navigation_safety_response_error",
            metadata={
                "topic": topic,
                "session_url": navigation_runtime.live_session_url,
                "error": str(exc.detail),
            },
        )
        return
    except Exception as exc:  # noqa: BLE001
        navigation_runtime.status = "paused"
        await _send_flow_message(
            websocket,
            text=(
                "I hit an unexpected issue while processing your safety reply. "
                "Please try yes or no again."
            ),
            reason="doc_navigation_safety_response_error",
            metadata={
                "topic": topic,
                "session_url": navigation_runtime.live_session_url,
                "error": str(exc),
            },
        )
        return

    navigation_runtime.live_session_url = result.debug_url or navigation_runtime.live_session_url
    visited_url = _best_step_url(result.steps) or result.debug_url
    steps_payload = _steps_payload(result.steps)
    if steps_payload:
        navigation_runtime.active_steps = steps_payload

    if result.status == "awaiting_confirmation":
        navigation_runtime.status = "paused"
        navigation_runtime.paused_summary = result.error or ""
        navigation_runtime.paused_url = visited_url
        navigation_runtime.paused_steps = steps_payload
        _set_pending_doc_navigation_confirmation(
            navigation_runtime,
            run_id=result.run_id,
            pending_confirmation=result.pending_confirmation,
        )
        await _send_flow_message(
            websocket,
            text=(
                "I reached another safety-gated browser action. "
                "Say yes to continue, or no to stop."
            ),
            reason="doc_navigation_safety_pause",
            metadata={
                "topic": topic,
                "visited_url": visited_url,
                "session_url": navigation_runtime.live_session_url,
                "steps": steps_payload,
                "run_id": result.run_id,
                "pending_confirmation": (
                    result.pending_confirmation.model_dump()
                    if result.pending_confirmation
                    else None
                ),
            },
        )
        return

    _clear_pending_doc_navigation_confirmation(navigation_runtime)

    if result.status in {"failed", "safety_denied"}:
        navigation_runtime.status = "failed"
        _reset_guided_use_case_mode(dialogue_state)
        await _send_flow_message(
            websocket,
            text=(
                "I won’t continue browser automation from here. "
                "I’ll still guide you with grounded documentation details."
            ),
            reason="doc_navigation_failed_status_fallback",
            metadata={
                "topic": topic,
                "visited_url": visited_url,
                "session_url": navigation_runtime.live_session_url,
                "status": result.status,
                "error": result.error,
            },
        )
        dialogue_state.branch_context = "doc_navigation_fallback"
        _send_text_to_agent(
            live_request_queue,
            (
                "Flow update: browser navigation ended while handling a safety decision.\n"
                f"Topic: {topic}\n"
                f"Status: {result.status}\n"
                f"Error: {result.error or 'N/A'}\n"
                "Continue with a concise deep explanation using grounded sources."
            ),
        )
        return

    navigation_runtime.status = "completed"
    navigation_runtime.paused_summary = result.final_reasoning or ""
    navigation_runtime.paused_url = visited_url
    navigation_runtime.paused_steps = steps_payload

    # ── Readiness gate (same as primary flow) ──
    use_case_ready = _is_use_case_section_ready(
        visited_url=visited_url,
        final_reasoning=result.final_reasoning,
        steps=steps_payload,
    )

    await _send_flow_message(
        websocket,
        text=(
            "I continued the documentation walkthrough."
            + (
                " I found the use-case section."
                if use_case_ready
                else " I couldn't locate a dedicated use-case section."
            )
            + (f" Current page: {visited_url}" if visited_url else "")
        ),
        reason="doc_navigation_locating_use_cases" if not use_case_ready else "doc_use_case_ready",
        metadata={
            "topic": topic,
            "visited_url": visited_url,
            "session_url": navigation_runtime.live_session_url,
            "steps": steps_payload,
            "use_case_ready": use_case_ready,
        },
    )

    if use_case_ready:
        _enable_guided_use_case_mode(
            dialogue_state,
            topic=topic,
            visited_url=visited_url,
            summary=result.final_reasoning,
        )
        dialogue_state.branch_context = "doc_navigation_explaining"
        _send_text_to_agent(
            live_request_queue,
            _build_guided_use_case_prompt(
                dialogue_state=dialogue_state,
                mode="start",
            ),
        )
    else:
        dialogue_state.branch_context = "doc_navigation_fallback_no_use_cases"
        _send_text_to_agent(
            live_request_queue,
            (
                f"Flow update: browser navigation resumed after safety and completed for {topic}, "
                f"but could not locate a dedicated use-case section.\n"
                f"Visited URL: {visited_url or 'unknown'}\n"
                f"Navigation summary: {result.final_reasoning or 'N/A'}\n"
                "Explain the topic concisely using grounded sources."
            ),
        )


async def _handle_text_turn(
    websocket: WebSocket,
    live_request_queue: LiveRequestQueue,
    dialogue_state: TutorDialogueState,
    navigation_runtime: DocNavigationRuntime,
    barge_in_runtime: BargeInRuntime | None,
    text_data: str,
) -> None:
    normalized_text = normalize_text(text_data)

    if _has_pending_doc_navigation_confirmation(navigation_runtime):
        dialogue_state.branch_context = "doc_navigation_safety_confirmation"
        if is_affirmative(normalized_text):
            await _resume_doc_navigation_after_safety_confirmation(
                websocket=websocket,
                live_request_queue=live_request_queue,
                dialogue_state=dialogue_state,
                navigation_runtime=navigation_runtime,
                acknowledged=True,
            )
            return
        if is_negative(normalized_text):
            await _resume_doc_navigation_after_safety_confirmation(
                websocket=websocket,
                live_request_queue=live_request_queue,
                dialogue_state=dialogue_state,
                navigation_runtime=navigation_runtime,
                acknowledged=False,
            )
            return
        await _send_flow_message(
            websocket,
            text=(
                "I’m paused at a safety check in the browser. "
                "Please say yes to continue, or no to stop browser actions."
            ),
            reason="doc_navigation_safety_confirmation_unclear",
            metadata={
                "topic": navigation_runtime.topic or dialogue_state.current_topic,
                "pending_confirmation": {
                    "action": navigation_runtime.pending_confirmation_action,
                    "step_index": navigation_runtime.pending_confirmation_step,
                    "explanation": navigation_runtime.pending_confirmation_explanation,
                },
            },
        )
        return

    if navigation_runtime.status == "paused" and is_resume_request(normalized_text):
        dialogue_state.branch_context = "doc_navigation_resuming"
        navigation_runtime.status = "completed"
        await _send_flow_message(
            websocket,
            text=(
                "Resuming from where we paused."
                + (
                    f" We were on {navigation_runtime.paused_url}."
                    if navigation_runtime.paused_url
                    else ""
                )
            ),
            reason="doc_navigation_resumed",
            metadata={
                "topic": navigation_runtime.topic,
                "visited_url": navigation_runtime.paused_url,
                "steps": navigation_runtime.paused_steps,
            },
        )
        _send_text_to_agent(
            live_request_queue,
            (
                "Flow update: user resumed paused doc walkthrough.\n"
                f"Topic: {navigation_runtime.topic or dialogue_state.current_topic or 'general'}\n"
                f"Visited URL: {navigation_runtime.paused_url or 'unknown'}\n"
                f"Navigation summary: {navigation_runtime.paused_summary or 'N/A'}\n"
                "Continue the explanation from this point in a concise, spoken-friendly way."
            ),
        )
        return

    if navigation_runtime.is_running() and not is_resume_request(normalized_text):
        if is_negative(normalized_text):
            navigation_runtime.pause_requested = True
            _clear_doc_progress_narration_state(navigation_runtime)
            dialogue_state.branch_context = "doc_navigation_pause_requested"
            await _send_flow_message(
                websocket,
                text=(
                    "Understood. I’ll pause the doc walkthrough after the current step. "
                    "You can say continue anytime."
                ),
                reason="doc_navigation_pause_requested",
                metadata={"topic": navigation_runtime.topic},
            )
            return

        navigation_runtime.pause_requested = True
        _clear_doc_progress_narration_state(navigation_runtime)
        await _send_flow_message(
            websocket,
            text=(
                "I heard your interruption. I’ll pause the doc walkthrough after this step, "
                "answer you now, and you can say continue later."
            ),
            reason="doc_navigation_interrupted",
            metadata={"topic": navigation_runtime.topic},
        )

    if (
        dialogue_state.guided_use_case_mode
        and not navigation_runtime.is_running()
        and not _has_pending_doc_navigation_confirmation(navigation_runtime)
    ):
        current_topic = dialogue_state.guided_use_case_topic or dialogue_state.current_topic
        requested_topic = infer_cloud_service_topic(text_data)
        if (
            requested_topic
            and current_topic
            and normalize_text(requested_topic) != normalize_text(current_topic)
        ):
            _reset_guided_use_case_mode(dialogue_state)
        else:
            if is_stop_use_case_request(normalized_text) or is_negative(normalized_text):
                _reset_guided_use_case_mode(dialogue_state)
                dialogue_state.branch_context = "doc_use_case_stopped"
                await _send_flow_message(
                    websocket,
                    text=(
                        "No problem. I’ll pause the use-case walkthrough here. "
                        "Whenever you want, say show me more and I’ll continue."
                    ),
                    reason="doc_use_case_stopped",
                    metadata={"topic": current_topic},
                )
                return

            if (
                is_next_use_case_request(normalized_text)
                or is_resume_request(normalized_text)
                or is_more_details_request(normalized_text)
            ):
                dialogue_state.branch_context = "doc_use_case_next"
                next_index = max(1, dialogue_state.guided_use_case_index + 1)
                await _send_flow_message(
                    websocket,
                    text=(
                        f"Perfect. I’m moving to use case {next_index} now. "
                        "I’ll stop there and ask if you have questions before we continue."
                    ),
                    reason="doc_use_case_advancing",
                    metadata={
                        "topic": current_topic,
                        "index": next_index,
                        "visited_url": dialogue_state.guided_use_case_url,
                    },
                )
                navigation_runtime.topic = current_topic
                navigation_runtime.provider = default_computer_use_provider()
                navigation_runtime.status = "launching"
                navigation_runtime.task = asyncio.create_task(
                    _run_doc_use_case_advance_flow(
                        websocket=websocket,
                        live_request_queue=live_request_queue,
                        dialogue_state=dialogue_state,
                        navigation_runtime=navigation_runtime,
                        current_index=dialogue_state.guided_use_case_index,
                        barge_in_runtime=barge_in_runtime,
                    )
                )
                return

            dialogue_state.branch_context = "doc_use_case_qa"
            _send_text_to_agent(
                live_request_queue,
                _build_guided_use_case_prompt(
                    dialogue_state=dialogue_state,
                    mode="qa",
                    user_text=text_data,
                ),
            )
            return

    awaiting_confirmation = dialogue_state.awaiting_doc_confirmation
    intent = detect_intent(text_data, awaiting_confirmation=awaiting_confirmation)
    dialogue_state.last_intent = intent
    if awaiting_confirmation and intent in {
        "confirm_yes",
        "confirm_no",
        "confirm_unclear",
    }:
        # Keep prior topic when the user just answers yes/no to confirmation.
        pass
    else:
        dialogue_state.current_topic = infer_topic(text_data, dialogue_state.current_topic)
        if awaiting_confirmation:
            # The user provided a completely new query, effectively dropping out of the doc confirmation loop.
            dialogue_state.awaiting_doc_confirmation = False

    if intent == "confirm_unclear":
        await _send_flow_message(
            websocket,
            text=(
                "For safety reasons, please approve launch so I can open the browser and "
                "guide you through the docs. You can say yes or no, or use the approve button."
            ),
            reason="confirmation_unclear",
        )
        return

    if intent == "end_conversation":
        await _send_flow_message(
            websocket,
            text="Ending the session. Goodbye!",
            reason="end_conversation",
        )
        _send_text_to_agent(
            live_request_queue,
            "The user explicitly asked to end the conversation. Say a very quick, warm goodbye."
        )

        async def _delayed_close() -> None:
            await asyncio.sleep(4.0)
            try:
                await websocket.close(code=1000, reason="user_ended_conversation")
            except Exception:
                pass

        asyncio.create_task(_delayed_close())
        return

    if intent == "confirm_no":
        dialogue_state.awaiting_doc_confirmation = False
        dialogue_state.branch_context = "doc_exploration_declined"
        followup_prompt = (
            "Flow update: user declined doc exploration.\n"
            f"Topic: {dialogue_state.current_topic or 'general'}\n"
            "Respond with a concise clarification and ask one short follow-up question."
        )
        _send_text_to_agent(live_request_queue, followup_prompt)
        return

    if intent == "confirm_yes":
        dialogue_state.awaiting_doc_confirmation = False
        dialogue_state.branch_context = "doc_exploration_confirmed"
        if navigation_runtime.is_running():
            await _send_flow_message(
                websocket,
                text="I’m already navigating docs for you. I’ll keep going.",
                reason="doc_navigation_already_running",
                metadata={"topic": navigation_runtime.topic},
            )
            return

        navigation_runtime.topic = _resolve_doc_topic(
            dialogue_state=dialogue_state,
            navigation_runtime=navigation_runtime,
            fallback_text=text_data,
        )
        navigation_runtime.provider = default_computer_use_provider()
        navigation_runtime.status = "launching"
        await _send_flow_message(
            websocket,
            text=(
                f"Awesome. Confirmed. I’m launching the documentation walkthrough for "
                f"{navigation_runtime.topic} now."
            ),
            reason="doc_navigation_launching",
            metadata={
                "topic": navigation_runtime.topic,
                "provider": navigation_runtime.provider,
                "target_url": _topic_to_docs_hint(navigation_runtime.topic or ""),
            },
        )
        
        _send_text_to_agent(
            live_request_queue,
            "Flow update: Navigation is starting.\n"
            "You MUST immediately say exactly: 'I am navigating to the official document.' and say nothing else.\n"
            "Do NOT ask any questions. Do NOT explain what you are doing. Just say that exact sentence."
        )

        navigation_runtime.task = asyncio.create_task(
            _run_doc_navigation_flow(
                websocket=websocket,
                live_request_queue=live_request_queue,
                dialogue_state=dialogue_state,
                navigation_runtime=navigation_runtime,
                barge_in_runtime=barge_in_runtime,
            )
        )
        return

    if intent == "request_more_details":
        dialogue_state.awaiting_doc_confirmation = True
        dialogue_state.branch_context = "awaiting_doc_confirmation"
        topic = _resolve_doc_topic(
            dialogue_state=dialogue_state,
            navigation_runtime=navigation_runtime,
            fallback_text=text_data,
        )
        navigation_runtime.topic = topic
        await _send_flow_message(
            websocket,
            text=f"I can open official docs and go deeper on \"{topic}\". Should I continue? Reply yes or no.",
            reason="doc_confirmation_required",
        )
        return

    if intent == "ambiguous":
        await _send_flow_message(
            websocket,
            text="I can help with cloud topics, architecture, or troubleshooting. What would you like to focus on?",
            reason="ambiguous_fallback",
        )
        return

    require_grounding = should_ground_query(
        text_data, previous_topic=dialogue_state.current_topic
    )
    dialogue_state.branch_context = (
        "grounded_qna" if require_grounding else "standard_qna"
    )
    _send_text_to_agent(
        live_request_queue,
        _build_stateful_prompt(
            text_data,
            dialogue_state,
            require_grounding=require_grounding,
        ),
    )


async def _client_to_agent_messaging(
    websocket: WebSocket,
    user_id: str,
    session_id: str,
    live_request_queue: LiveRequestQueue,
    dialogue_state: TutorDialogueState,
    navigation_runtime: DocNavigationRuntime,
    barge_in_runtime: BargeInRuntime,
    allow_activity_signals: bool,
) -> None:
    """Reads client websocket messages and pushes to live queue."""
    while True:
        ws_message = await websocket.receive()

        if ws_message.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect

        binary_chunk = ws_message.get("bytes")
        if binary_chunk:
            # Binary audio frames are preferred for throughput.
            live_request_queue.send_realtime(
                Blob(data=binary_chunk, mime_type="audio/pcm;rate=16000")
            )
            continue

        raw = ws_message.get("text")
        if not raw:
            continue

        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "invalid_json",
                    "message": "Expected JSON text frame or binary audio frame.",
                    "time_utc": utc_now_iso(),
                }
            )
            continue

        # Local ping path: does not hit model.
        if message.get("type") == "ping":
            await websocket.send_json({"type": "pong", "time_utc": utc_now_iso()})
            continue

        if message.get("type") == "client_speech_detected":
            if not barge_in_runtime.agent_output_in_progress:
                # Ignore frontend VAD interrupts while the user is just speaking
                # their own request and the agent is not currently talking.
                continue
            if barge_in_runtime.suppress_agent_output:
                continue
            now = time.monotonic()
            if now - barge_in_runtime.last_client_interrupt_at_monotonic < 0.35:
                continue
            barge_in_runtime.last_client_interrupt_at_monotonic = now
            barge_in_runtime.suppress_agent_output = True
            barge_in_runtime.suppress_started_at_monotonic = now
            LOGGER.info(
                "Frontend explicitly halted generation via client_speech_detected signal"
            )
            # Per ADK streaming guide: activity_start/end should only be used when
            # automatic VAD is disabled. With automatic VAD enabled, continuous
            # realtime audio is enough for barge-in.
            if allow_activity_signals:
                live_request_queue.send_activity_start()
                live_request_queue.send_activity_end()
            try:
                await websocket.send_json(
                    {
                        "type": "interrupted",
                        "reason": "client_speech_detected",
                        "time_utc": utc_now_iso(),
                    }
                )
            except Exception:  # noqa: BLE001
                pass
            continue

        mime_type = message.get("mime_type")
        if not mime_type and message.get("type") == "audio":
            mime_type = "audio/pcm;rate=16000"

        if mime_type == "text/plain":
            text_data = (message.get("data") or "").strip()
            if text_data:
                # New user text input should always allow the next answer through.
                barge_in_runtime.suppress_agent_output = False
                barge_in_runtime.agent_output_in_progress = False
                if _should_route_voice_control_text(
                    text_data=text_data,
                    dialogue_state=dialogue_state,
                    navigation_runtime=navigation_runtime,
                ):
                    normalized_control = _normalize_control_text(text_data)
                    if _was_recent_control_text(
                        navigation_runtime, normalized_control
                    ):
                        continue
                    _mark_control_text_seen(navigation_runtime, normalized_control)
                try:
                    session_persistence_manager.record_user_message(
                        user_id=user_id,
                        session_id=session_id,
                        text=text_data,
                        dialogue_state=dialogue_state.snapshot(),
                        metadata={"branch_context": dialogue_state.branch_context or ""},
                    )
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("Failed to persist user message: %s", exc)
                await _handle_text_turn(
                    websocket=websocket,
                    live_request_queue=live_request_queue,
                    dialogue_state=dialogue_state,
                    navigation_runtime=navigation_runtime,
                    barge_in_runtime=barge_in_runtime,
                    text_data=text_data,
                )
                try:
                    session_persistence_manager.save_dialogue_state(
                        user_id=user_id,
                        session_id=session_id,
                        dialogue_state=dialogue_state.snapshot(),
                    )
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("Failed to persist dialogue state: %s", exc)
            continue

        if mime_type and mime_type.startswith("audio/pcm"):
            data = message.get("data") or ""
            decoded_data = base64.b64decode(data)
            normalized_mime_type = (
                mime_type if "rate=" in mime_type else "audio/pcm;rate=16000"
            )
            live_request_queue.send_realtime(
                Blob(data=decoded_data, mime_type=normalized_mime_type)
            )
            continue

        if mime_type == "image/jpeg":
            data = message.get("data") or ""
            decoded_data = base64.b64decode(data)
            live_request_queue.send_realtime(Blob(data=decoded_data, mime_type=mime_type))
            continue

        await websocket.send_json(
            {
                "type": "error",
                "code": "unsupported_message_type",
                "message": f"Unsupported client message: {message}",
                "time_utc": utc_now_iso(),
            }
        )


async def _agent_to_client_messaging(
    websocket: WebSocket,
    user_id: str,
    session_id: str,
    live_events: Any,
    live_request_queue: LiveRequestQueue,
    dialogue_state: TutorDialogueState,
    navigation_runtime: DocNavigationRuntime,
    barge_in_runtime: BargeInRuntime,
) -> None:
    """Streams ADK events to websocket client."""
    async for event in live_events:
        if (
            barge_in_runtime.suppress_agent_output
            and time.monotonic() - barge_in_runtime.suppress_started_at_monotonic > 2.5
        ):
            # Safety valve: never let suppression stick indefinitely.
            barge_in_runtime.suppress_agent_output = False
        message = _build_event_message(event)
        if message is not None:
            input_transcription = message.get("input_transcription") or {}
            input_text = (
                str(input_transcription.get("text", "")).strip()
                if input_transcription.get("is_final")
                else ""
            )
            if input_text:
                # As soon as a new final user utterance is recognized, clear any
                # leftover barge-in suppression so the next agent reply does not
                # lose its opening words.
                barge_in_runtime.suppress_agent_output = False
                barge_in_runtime.agent_output_in_progress = False
            if input_text and _should_route_voice_control_text(
                text_data=input_text,
                dialogue_state=dialogue_state,
                navigation_runtime=navigation_runtime,
            ):
                normalized_control = _normalize_control_text(input_text)
                if not _was_recent_control_text(
                    navigation_runtime, normalized_control
                ):
                    # Suppress stale "default" model follow-up output for control
                    # utterances (e.g., "show me more", "yes") so flow replies drive UX.
                    barge_in_runtime.suppress_agent_output = True
                    barge_in_runtime.suppress_started_at_monotonic = time.monotonic()
                    barge_in_runtime.agent_output_in_progress = False
                    _mark_control_text_seen(navigation_runtime, normalized_control)
                    try:
                        session_persistence_manager.record_user_message(
                            user_id=user_id,
                            session_id=session_id,
                            text=input_text,
                            dialogue_state=dialogue_state.snapshot(),
                            metadata={
                                "branch_context": dialogue_state.branch_context or "",
                                "source": "server_voice_fallback",
                            },
                        )
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.warning(
                            "Failed to persist fallback voice control message: %s", exc
                        )
                    await _handle_text_turn(
                        websocket=websocket,
                        live_request_queue=live_request_queue,
                        dialogue_state=dialogue_state,
                        navigation_runtime=navigation_runtime,
                        barge_in_runtime=barge_in_runtime,
                        text_data=input_text,
                    )
                    try:
                        session_persistence_manager.save_dialogue_state(
                            user_id=user_id,
                            session_id=session_id,
                            dialogue_state=dialogue_state.snapshot(),
                        )
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.warning("Failed to persist dialogue state: %s", exc)

            if (
                dialogue_state.branch_context == "grounded_qna"
                and not message.get("citations")
            ):
                message["citations"] = _fallback_citations_for_topic(
                    dialogue_state.current_topic or ""
                )

            message_output_text = _extract_message_output_text(message)
            if _should_suppress_navigation_detour_output(
                output_text=message_output_text,
                dialogue_state=dialogue_state,
                navigation_runtime=navigation_runtime,
            ):
                barge_in_runtime.suppress_agent_output = True
                barge_in_runtime.suppress_started_at_monotonic = time.monotonic()
                continue

            input_transcription = message.get("input_transcription") or {}
            has_input_text = bool(input_transcription.get("text"))
            has_agent_output = _message_contains_agent_output(message)
            if has_agent_output and not message.get("turn_complete"):
                barge_in_runtime.agent_output_in_progress = True
            if message.get("interrupted") or message.get("turn_complete"):
                barge_in_runtime.agent_output_in_progress = False
            if barge_in_runtime.suppress_agent_output:
                if message.get("interrupted") and not has_input_text:
                    barge_in_runtime.suppress_agent_output = False
                    try:
                        await websocket.send_json(
                            {
                                "type": "interrupted",
                                "reason": "model_interrupted",
                                "time_utc": utc_now_iso(),
                            }
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                if message.get("turn_complete") and not has_input_text:
                    barge_in_runtime.suppress_agent_output = False
                if has_agent_output:
                    continue

            output_transcription = message.get("output_transcription") or {}
            if output_transcription.get("is_final") and output_transcription.get("text"):
                final_text = str(output_transcription["text"])
                dialogue_state.last_answer = final_text[:4000]
                if _agent_indicates_doc_navigation_started(final_text):
                    dialogue_state.awaiting_doc_confirmation = False
                elif (
                    _agent_requests_doc_confirmation(final_text)
                    and not navigation_runtime.is_running()
                    and navigation_runtime.status in {"idle", "completed", "failed"}
                ):
                    dialogue_state.awaiting_doc_confirmation = True
                    dialogue_state.branch_context = "awaiting_doc_confirmation"
                try:
                    session_persistence_manager.record_agent_message(
                        user_id=user_id,
                        session_id=session_id,
                        text=final_text,
                        citations=message.get("citations") or [],
                        dialogue_state=dialogue_state.snapshot(),
                        metadata={"branch_context": dialogue_state.branch_context or ""},
                    )
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("Failed to persist agent message: %s", exc)
            elif message.get("turn_complete") and message.get("citations"):
                try:
                    session_persistence_manager.record_agent_message(
                        user_id=user_id,
                        session_id=session_id,
                        text="",
                        citations=message.get("citations") or [],
                        dialogue_state=dialogue_state.snapshot(),
                        metadata={"branch_context": dialogue_state.branch_context or ""},
                    )
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("Failed to persist citations: %s", exc)
            await websocket.send_text(json.dumps(message))

        for _mime_type, audio_chunk in _extract_binary_audio_chunks(event):
            if barge_in_runtime.suppress_agent_output:
                continue
            await websocket.send_bytes(audio_chunk)


async def _run_agent_stream_loop(
    *,
    websocket: WebSocket,
    user_id: str,
    session_id: str,
    live_request_queue: LiveRequestQueue,
    run_config: RunConfig,
    dialogue_state: TutorDialogueState,
    navigation_runtime: DocNavigationRuntime,
    barge_in_runtime: BargeInRuntime,
) -> None:
    live_events = runner.run_live(
        user_id=user_id,
        session_id=session_id,
        live_request_queue=live_request_queue,
        run_config=run_config,
    )
    await _agent_to_client_messaging(
        websocket=websocket,
        user_id=user_id,
        session_id=session_id,
        live_events=live_events,
        live_request_queue=live_request_queue,
        dialogue_state=dialogue_state,
        navigation_runtime=navigation_runtime,
        barge_in_runtime=barge_in_runtime,
    )


def _hydrate_dialogue_state_from_snapshot(
    dialogue_state: TutorDialogueState,
    snapshot: SessionSnapshotResponse,
) -> None:
    raw = snapshot.dialogue_state or {}
    topic = str(raw.get("current_topic") or "").strip()
    last_answer = str(raw.get("last_answer") or "").strip()
    branch_context = str(raw.get("branch_context") or "").strip()
    last_intent = str(raw.get("last_intent") or "").strip()
    guided_use_case_topic = str(raw.get("guided_use_case_topic") or "").strip()
    guided_use_case_url = str(raw.get("guided_use_case_url") or "").strip()
    guided_use_case_summary = str(raw.get("guided_use_case_summary") or "").strip()
    guided_use_case_index_raw = raw.get("guided_use_case_index", 0)
    try:
        guided_use_case_index = int(guided_use_case_index_raw)
    except (TypeError, ValueError):
        guided_use_case_index = 0

    dialogue_state.current_topic = topic or None
    dialogue_state.last_answer = last_answer or None
    dialogue_state.branch_context = branch_context or None
    dialogue_state.awaiting_doc_confirmation = bool(
        raw.get("awaiting_doc_confirmation", False)
    )
    dialogue_state.last_intent = last_intent or "unknown"
    dialogue_state.guided_use_case_mode = bool(raw.get("guided_use_case_mode", False))
    dialogue_state.guided_use_case_ready = bool(raw.get("guided_use_case_ready", False))
    dialogue_state.guided_use_case_index = max(0, guided_use_case_index)
    dialogue_state.guided_use_case_topic = guided_use_case_topic or None
    dialogue_state.guided_use_case_url = guided_use_case_url or None
    dialogue_state.guided_use_case_summary = guided_use_case_summary or None


@app.websocket("/ws/{user_id}/{session_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    user_id: str,
    session_id: str,
    local_time: str | None = None,
    tz: str | None = None,
) -> None:
    """ADK live websocket endpoint."""
    await websocket.accept()

    connection_id = f"{user_id}:{session_id}:{uuid_short()}"
    LOGGER.info("WebSocket connected %s", connection_id)

    await websocket.send_json(
        {
            "type": "connection",
            "status": "connected",
            "connection_id": connection_id,
            "session": {"user_id": user_id, "session_id": session_id},
            "model": root_agent.model,
            "time_utc": utc_now_iso(),
        }
    )

    await _get_or_create_session(user_id=user_id, session_id=session_id)
    live_request_queue = LiveRequestQueue()
    dialogue_state = TutorDialogueState()
    navigation_runtime = DocNavigationRuntime()
    barge_in_runtime = BargeInRuntime()
    run_config = _build_run_config()
    allow_activity_signals = _allow_manual_activity_signals(run_config)
    restored_snapshot: SessionSnapshotResponse | None = None
    try:
        restored_snapshot = session_persistence_manager.load_session(
            user_id=user_id, session_id=session_id
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Failed to load persisted session snapshot: %s", exc)
    if restored_snapshot is not None:
        _hydrate_dialogue_state_from_snapshot(dialogue_state, restored_snapshot)
        await websocket.send_json(
            {
                "type": "session_state",
                "status": "restored",
                "event_count": restored_snapshot.event_count,
                "topic": dialogue_state.current_topic,
                "time_utc": utc_now_iso(),
            }
        )
    else:
        try:
            session_persistence_manager.save_dialogue_state(
                user_id=user_id,
                session_id=session_id,
                dialogue_state=dialogue_state.snapshot(),
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to persist initial dialogue state: %s", exc)
    try:
        session_persistence_manager.record_system_event(
            user_id=user_id,
            session_id=session_id,
            event_type="ws_connected",
            dialogue_state=dialogue_state.snapshot(),
            metadata={"connection_id": connection_id},
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Failed to persist ws_connected event: %s", exc)

    upstream_task = asyncio.create_task(
        _client_to_agent_messaging(
            websocket,
            user_id,
            session_id,
            live_request_queue,
            dialogue_state,
            navigation_runtime,
            barge_in_runtime,
            allow_activity_signals,
        )
    )

    if local_time:
        time_msg = f"SYSTEM: The user just connected. Their local time is {local_time}."
        if tz:
            time_msg += f" Their timezone identifier is {tz} (for internal context only)."
        time_msg += (
            "\nCRITICAL RULE: You MUST immediately speak first and greet them. "
            "Make the greeting wildly enthusiastic, warm, and acknowledge their time of day. "
            "Do not assume or mention a city/country from timezone text unless the user explicitly asks. "
            "BE CREATIVE. Do not use the exact same greeting every time."
        )
        _send_text_to_agent(live_request_queue, time_msg)
    downstream_task = asyncio.create_task(
        _run_agent_stream_loop(
            websocket=websocket,
            user_id=user_id,
            session_id=session_id,
            live_request_queue=live_request_queue,
            run_config=run_config,
            dialogue_state=dialogue_state,
            navigation_runtime=navigation_runtime,
            barge_in_runtime=barge_in_runtime,
        )
    )

    try:
        done, pending = await asyncio.wait(
            {upstream_task, downstream_task},
            return_when=asyncio.FIRST_EXCEPTION,
        )

        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                raise exc

        for task in pending:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    except WebSocketDisconnect:
        LOGGER.info("WebSocket disconnected %s", connection_id)
        try:
            session_persistence_manager.record_system_event(
                user_id=user_id,
                session_id=session_id,
                event_type="ws_disconnected",
                dialogue_state=dialogue_state.snapshot(),
                metadata={"connection_id": connection_id},
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to persist ws_disconnected event: %s", exc)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("WebSocket loop failed for %s", connection_id)
        try:
            session_persistence_manager.record_system_event(
                user_id=user_id,
                session_id=session_id,
                event_type="ws_error",
                text=str(exc),
                dialogue_state=dialogue_state.snapshot(),
                metadata={"connection_id": connection_id},
            )
        except Exception as persist_exc:  # noqa: BLE001
            LOGGER.warning("Failed to persist ws_error event: %s", persist_exc)
        try:
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "server_exception",
                    "message": str(exc),
                    "time_utc": utc_now_iso(),
                }
            )
        except Exception:  # noqa: BLE001
            pass
    finally:
        if navigation_runtime.task and not navigation_runtime.task.done():
            navigation_runtime.task.cancel()
            await asyncio.gather(navigation_runtime.task, return_exceptions=True)
        try:
            session_persistence_manager.save_dialogue_state(
                user_id=user_id,
                session_id=session_id,
                dialogue_state=dialogue_state.snapshot(),
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to persist final dialogue state: %s", exc)
        live_request_queue.close()


def uuid_short() -> str:
    """Short random id for connection logs."""
    return os.urandom(3).hex()
