"""Session 08 tutorial artifact generation and local persistence helpers."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


TRANSCRIPT_TIMESTAMP_PATTERN = re.compile(r"^\[[^\]]+\]\s*")
SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?])\s+")
NON_WORD_PATTERN = re.compile(r"[^a-z0-9]+")
SPACES_PATTERN = re.compile(r"\s+")

CONVERSATIONAL_SNIPPETS = (
    "would you like",
    "are you interested",
    "show me more",
    "i can",
    "let me",
    "do you want",
    "can you",
    "feel free",
    "anything else",
)

TOPIC_PATTERNS_FROM_URL = (
    ("functions/docs/triggers/http", "Cloud Functions HTTP triggers"),
    ("cloudfunctions/docs", "Cloud Functions"),
    ("cloudfunctions.google.com", "Cloud Functions"),
    ("cloud run", "Cloud Run"),
    ("/run/docs", "Cloud Run"),
    ("kubernetes-engine", "Google Kubernetes Engine"),
    ("bigquery", "BigQuery"),
    ("firebase.google.com/docs/firestore", "Firestore"),
    ("firebase.google.com/docs/hosting", "Firebase Hosting"),
    ("firebase.google.com/docs", "Firebase"),
    ("vertex-ai", "Vertex AI"),
    ("storage/docs", "Cloud Storage"),
    ("cloud.google.com/storage", "Cloud Storage"),
    ("cloud.google.com/build", "Cloud Build"),
)

LOW_INFORMATION_TOPIC_PATTERNS = (
    "sure",
    "sure thank you",
    "thank you",
    "thanks",
    "thanks you",
    "hi",
    "hello",
    "hey",
    "hey what s up",
    "what s up",
    "whats up",
    "how are you",
    "good morning",
    "good afternoon",
    "good evening",
    "ok",
    "okay",
    "yes",
    "no",
    "continue",
    "go on",
    "sounds good",
    "got it",
)

GREETING_TOKENS = {"hi", "hello", "hey", "yo"}

CLOUD_SIGNAL_PATTERNS = (
    "cloud",
    "gcp",
    "google cloud",
    "cloud function",
    "cloud run",
    "bigquery",
    "firestore",
    "firebase",
    "vertex ai",
    "gke",
    "kubernetes",
    "iam",
    "pubsub",
    "pub/sub",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


LOGGER = logging.getLogger("cloudtutor.artifacts")


class ArtifactCitation(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    url: str = Field(min_length=1, max_length=2000)


class TutorialArtifactCreateRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    topic: str | None = Field(default=None, max_length=200)
    user_transcript: str = Field(default="", max_length=100_000)
    agent_transcript: str = Field(default="", max_length=100_000)
    citations: list[ArtifactCitation] = Field(default_factory=list)
    include_pdf: bool = False


class TutorialArtifactCreateResponse(BaseModel):
    artifact_id: str
    created_at: str
    topic: str
    summary: str
    key_points: list[str] = Field(default_factory=list)
    tutorial_steps: list[str] = Field(default_factory=list)
    check_for_understanding: str
    mermaid_diagram: str
    citations: list[ArtifactCitation] = Field(default_factory=list)
    html_path: str
    html_url: str
    pdf_path: str | None = None
    pdf_url: str | None = None
    cloud_html_url: str | None = None
    cloud_pdf_url: str | None = None
    storage_provider: str | None = None
    notes: list[str] = Field(default_factory=list)


@dataclass
class TutorialArtifactRecord:
    artifact_id: str
    created_at: str
    topic: str
    html_path: Path
    pdf_path: Path | None
    response_payload: TutorialArtifactCreateResponse


def _slugify(value: str, limit: int = 40) -> str:
    lowered = value.strip().lower()
    cleaned = NON_WORD_PATTERN.sub("-", lowered).strip("-")
    if not cleaned:
        return "topic"
    return cleaned[:limit]


def _normalize_whitespace(value: str) -> str:
    return SPACES_PATTERN.sub(" ", value).strip()


def _parse_transcript_lines(raw_text: str) -> list[str]:
    lines: list[str] = []
    for line in raw_text.splitlines():
        cleaned = TRANSCRIPT_TIMESTAMP_PATTERN.sub("", line).strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def _normalize_sentence(sentence: str) -> str:
    cleaned = _normalize_whitespace(sentence)
    cleaned = cleaned.strip(" -")
    if not cleaned:
        return ""
    if cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def _is_low_information_topic(topic: str) -> bool:
    normalized = NON_WORD_PATTERN.sub(" ", topic.lower()).strip()
    normalized = _normalize_whitespace(normalized)
    if not normalized:
        return True

    if normalized in LOW_INFORMATION_TOPIC_PATTERNS:
        return True

    tokens = [token for token in normalized.split(" ") if token]
    reduced_tokens = [
        token
        for token in tokens
        if token not in {"you", "very", "much", "so", "the", "a", "an"}
    ]
    reduced = " ".join(reduced_tokens).strip()
    if reduced in LOW_INFORMATION_TOPIC_PATTERNS:
        return True

    if len(tokens) <= 4 and any(pattern in normalized for pattern in LOW_INFORMATION_TOPIC_PATTERNS):
        return True

    if len(tokens) <= 5 and tokens[0] in GREETING_TOKENS:
        return True

    if len(tokens) <= 5 and normalized.startswith("how are"):
        return True

    if len(tokens) <= 3 and all(token in LOW_INFORMATION_TOPIC_PATTERNS for token in tokens):
        return True

    for pattern in LOW_INFORMATION_TOPIC_PATTERNS:
        if normalized == pattern:
            return True
    return False


def _contains_cloud_signal(text: str) -> bool:
    normalized = NON_WORD_PATTERN.sub(" ", text.lower()).strip()
    normalized = _normalize_whitespace(normalized)
    return any(pattern in normalized for pattern in CLOUD_SIGNAL_PATTERNS)


def _is_conversational_sentence(sentence: str) -> bool:
    lowered = sentence.lower()
    if lowered.endswith("?") and "http" not in lowered:
        return True
    return any(snippet in lowered for snippet in CONVERSATIONAL_SNIPPETS)


def _extract_sentences(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    joined = " ".join(lines)
    for fragment in SENTENCE_SPLIT_PATTERN.split(joined):
        sentence = _normalize_sentence(fragment)
        if len(sentence) < 20:
            continue
        lowered = sentence.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        output.append(sentence)
    return output


def _infer_topic_from_citations(citations: list[ArtifactCitation]) -> str | None:
    for citation in citations:
        url = citation.url.lower()
        for pattern, topic in TOPIC_PATTERNS_FROM_URL:
            if pattern in url:
                return topic

    for citation in citations:
        title = citation.title.strip()
        if 3 <= len(title) <= 90:
            return title
    return None


def _infer_topic_from_text_lines(lines: list[str]) -> str | None:
    for line in reversed(lines):
        cleaned = _normalize_sentence(line)
        lowered = cleaned.lower()
        if len(cleaned) < 6:
            continue
        if "what is " in lowered:
            candidate = cleaned.split("?", 1)[0]
            return candidate.replace("What is ", "").replace("what is ", "").strip(" .")
        if "cloud function" in lowered:
            return "Cloud Functions"
        if "cloud run" in lowered:
            return "Cloud Run"
        if "firestore" in lowered:
            return "Firestore"
        if "bigquery" in lowered:
            return "BigQuery"
        if "vertex ai" in lowered:
            return "Vertex AI"
        if "gke" in lowered or "kubernetes engine" in lowered:
            return "Google Kubernetes Engine"
    return None


def _topic_category(topic: str) -> str:
    lowered = topic.lower()
    if "function" in lowered:
        return "cloud_functions"
    if "cloud run" in lowered:
        return "cloud_run"
    if "gke" in lowered or "kubernetes engine" in lowered:
        return "gke"
    if "bigquery" in lowered:
        return "bigquery"
    if "firestore" in lowered:
        return "firestore"
    if "firebase hosting" in lowered:
        return "firebase_hosting"
    if "vertex ai" in lowered:
        return "vertex_ai"
    if "cloud storage" in lowered:
        return "cloud_storage"
    return "general_cloud"


def _select_topic(
    requested_topic: str | None,
    citations: list[ArtifactCitation],
    user_lines: list[str],
    agent_lines: list[str],
) -> str:
    requested = _normalize_sentence(requested_topic or "")
    conversation_has_cloud_signal = _contains_cloud_signal(
        " ".join(user_lines + agent_lines)
    )
    requested_has_cloud_signal = _contains_cloud_signal(requested)
    if requested and not _is_low_information_topic(requested):
        if conversation_has_cloud_signal and not requested_has_cloud_signal:
            # Ignore vague/banter overrides when conversation clearly has a cloud topic.
            pass
        else:
            return requested[:160]

    from_citations = _infer_topic_from_citations(citations)
    if from_citations:
        return _normalize_sentence(from_citations)[:160]

    from_user = _infer_topic_from_text_lines(user_lines)
    if from_user:
        return _normalize_sentence(from_user)[:160]

    from_agent = _infer_topic_from_text_lines(agent_lines)
    if from_agent:
        return _normalize_sentence(from_agent)[:160]

    return "Cloud architecture topic"


def _build_summary(topic: str, agent_lines: list[str], user_lines: list[str]) -> str:
    sentences = _extract_sentences(agent_lines)
    informative = [s for s in sentences if not _is_conversational_sentence(s)]

    preferred = informative[0] if informative else ""
    if preferred:
        if not preferred.lower().startswith(topic.lower()):
            return f"{topic} in practice: {preferred[:340]}"
        return preferred[:340]

    if user_lines:
        return (
            f"{topic} is the focus of this tutorial, with practical steps for setup, "
            "verification, and reliable operation."
        )
    return (
        f"{topic} is explained here as an implementation guide covering architecture, "
        "core setup, and validation."
    )


def _default_key_points_for_category(topic: str, category: str) -> list[str]:
    if category == "cloud_functions":
        return [
            "Use HTTP triggers when you need function execution from web requests or webhooks.",
            "Design handler logic to validate input, return clear responses, and fail safely.",
            "Deploy with least-privilege IAM and explicit runtime/resource configuration.",
            "Use Cloud Logging and monitoring alerts to track execution health.",
        ]
    if category == "cloud_run":
        return [
            "Cloud Run is best for stateless containerized services with variable traffic.",
            "Container startup time and concurrency settings directly affect performance and cost.",
            "Use revision-based rollouts and health checks for safer deployments.",
            "Observe latency, error rate, and autoscaling metrics after release.",
        ]
    if category == "bigquery":
        return [
            "Model datasets/tables around analytical query patterns and partitioning.",
            "Control cost with partition pruning, clustering, and query optimization.",
            "Use IAM and authorized views to enforce least-privilege analytics access.",
            "Track query performance and slot usage with monitoring dashboards.",
        ]
    return [
        f"{topic} should be grounded in official documentation before implementation.",
        "Start with a minimal working setup to validate assumptions early.",
        "Add observability and safe rollout practices before scaling.",
        "Review security and IAM boundaries as part of the implementation plan.",
    ]


def _build_key_points(
    topic: str,
    category: str,
    agent_lines: list[str],
    citations: list[ArtifactCitation],
) -> list[str]:
    sentences = _extract_sentences(agent_lines)
    informative = [s for s in sentences if not _is_conversational_sentence(s)]
    key_points = informative[:4]

    if len(key_points) < 3:
        key_points = _default_key_points_for_category(topic, category)

    if citations:
        first = citations[0]
        key_points.append(
            f"Validate implementation choices against official docs: {first.title}."
        )

    # De-duplicate while preserving order.
    deduped: list[str] = []
    seen: set[str] = set()
    for point in key_points:
        normalized = _normalize_sentence(point)
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(normalized)

    return deduped[:5]


def _build_tutorial_steps(
    topic: str,
    category: str,
    citations: list[ArtifactCitation],
) -> list[str]:
    docs_step = "Review the official documentation and extract required prerequisites."
    if citations:
        docs_step = f"Review official docs first ({citations[0].title}) and confirm prerequisites."

    if category == "cloud_functions":
        return [
            "Define the HTTP request/response contract and function responsibility.",
            docs_step,
            "Implement the function handler with input validation and error handling.",
            "Deploy to Cloud Functions (2nd gen) with least-privilege IAM settings.",
            "Test the endpoint with curl/Postman and verify status, latency, and logs.",
            "Add monitoring and alerting thresholds, then iterate safely.",
        ]
    if category == "cloud_run":
        return [
            "Define service boundaries, expected traffic, and stateless behavior.",
            docs_step,
            "Build and containerize the service, then configure runtime variables.",
            "Deploy a Cloud Run revision with controlled concurrency and auth settings.",
            "Run smoke tests against the service URL and inspect request logs.",
            "Establish rollout and rollback policy with monitoring gates.",
        ]
    if category == "bigquery":
        return [
            "Define analytics goals, dataset structure, and data freshness requirements.",
            docs_step,
            "Create partitioned/clustered tables and load representative sample data.",
            "Write and optimize baseline queries with cost and latency checks.",
            "Set IAM access controls for analysts and service accounts.",
            "Track query efficiency and tune schema/query patterns iteratively.",
        ]
    return [
        f"Define the expected outcome and scope for {topic}.",
        docs_step,
        "Create a minimal working implementation and validate it end-to-end.",
        "Add security, IAM boundaries, and operational safeguards.",
        "Run validation tests and confirm performance against requirements.",
        "Introduce monitoring and rollback procedures before wider rollout.",
    ]


def _build_check_question(topic: str, category: str) -> str:
    if category == "cloud_functions":
        return (
            "How would you explain the difference between an HTTP-triggered function "
            "and a long-running service to a teammate?"
        )
    if category == "cloud_run":
        return (
            "What configuration choices in Cloud Run most affect cost and latency for your workload?"
        )
    return (
        f"What are the first 3 implementation checkpoints you would use to validate {topic} in production?"
    )


def _safe_mermaid_label(text: str, limit: int = 54) -> str:
    cleaned = text.replace('"', "'").replace("|", "/")
    cleaned = _normalize_whitespace(cleaned)
    if not cleaned:
        cleaned = "Step"
    return cleaned[:limit]


def _build_mermaid(topic: str, category: str) -> str:
    topic_label = _safe_mermaid_label(topic, limit=44)

    if category == "cloud_functions":
        return (
            "flowchart LR\n"
            '    A["Client / Webhook"] --> B["HTTP Trigger"]\n'
            '    B --> C["Cloud Functions (2nd gen)"]\n'
            '    C --> D["Business Logic"]\n'
            '    C --> E["Cloud Logging"]\n'
            '    C --> F["Monitoring & Alerts"]\n'
        )
    if category == "cloud_run":
        return (
            "flowchart LR\n"
            '    A["Client Request"] --> B["Cloud Run Service"]\n'
            '    B --> C["Container Revision"]\n'
            '    C --> D["Application Logic"]\n'
            '    B --> E["Cloud Logging"]\n'
            '    B --> F["Metrics & Autoscaling"]\n'
        )
    if category == "bigquery":
        return (
            "flowchart LR\n"
            '    A["Source Data"] --> B["BigQuery Dataset"]\n'
            '    B --> C["Partitioned Tables"]\n'
            '    C --> D["Analytical Queries"]\n'
            '    D --> E["Dashboards / Reports"]\n'
            '    B --> F["IAM + Governance"]\n'
        )

    return (
        "flowchart LR\n"
        f'    A["{topic_label}"] --> B["Implementation Plan"]\n'
        '    B --> C["Working Deployment"]\n'
        '    C --> D["Validation Tests"]\n'
        '    D --> E["Observability"]\n'
        '    E --> F["Iterative Improvement"]\n'
    )


def _render_html_document(
    *,
    artifact_id: str,
    created_at: str,
    topic: str,
    summary: str,
    key_points: list[str],
    tutorial_steps: list[str],
    check_for_understanding: str,
    mermaid_diagram: str,
    citations: list[ArtifactCitation],
) -> str:
    escaped_topic = html.escape(topic)
    escaped_summary = html.escape(summary)
    escaped_check = html.escape(check_for_understanding)
    key_points_html = "\n".join(f"<li>{html.escape(point)}</li>" for point in key_points)
    steps_html = "\n".join(f"<li>{html.escape(step)}</li>" for step in tutorial_steps)
    citations_html = "\n".join(
        (
            f'<li><a href="{html.escape(c.url)}" target="_blank" rel="noreferrer">'
            f"{html.escape(c.title)}</a></li>"
        )
        for c in citations
    )
    if not citations_html:
        citations_html = "<li>No source links were provided for this artifact.</li>"

    escaped_mermaid = html.escape(mermaid_diagram)
    escaped_created_at = html.escape(created_at)
    escaped_artifact_id = html.escape(artifact_id)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CloudTutor Tutorial Artifact</title>
  <style>
    :root {{
      --bg: #f5f9ff;
      --text: #122234;
      --muted: #506884;
      --panel: #ffffff;
      --line: #d8e3f2;
      --accent: #0a66c2;
    }}
    body {{
      margin: 0;
      background: radial-gradient(circle at 15% 0%, #deecff, var(--bg) 52%);
      color: var(--text);
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      line-height: 1.5;
    }}
    main {{
      max-width: 980px;
      margin: 28px auto;
      padding: 0 16px 48px;
    }}
    section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 16px;
      margin-top: 14px;
    }}
    h1, h2 {{
      margin: 0 0 8px;
    }}
    .meta {{
      color: var(--muted);
      font-size: 13px;
    }}
    a {{
      color: var(--accent);
      text-decoration: none;
    }}
    a:hover {{
      text-decoration: underline;
    }}
    .diagram {{
      background: #0f1a2a;
      color: #d8e7ff;
      border-radius: 10px;
      padding: 12px;
      overflow-x: auto;
      font-size: 13px;
    }}
  </style>
  <script type="module">
    import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
    mermaid.initialize({{ startOnLoad: true, securityLevel: "loose", theme: "default" }});
  </script>
</head>
<body>
  <main>
    <h1>CloudTutor Tutorial Artifact</h1>
    <p class="meta">Artifact ID: {escaped_artifact_id} | Generated: {escaped_created_at}</p>

    <section>
      <h2>Topic</h2>
      <p>{escaped_topic}</p>
      <h2>Summary</h2>
      <p>{escaped_summary}</p>
    </section>

    <section>
      <h2>Key Points</h2>
      <ul>{key_points_html}</ul>
    </section>

    <section>
      <h2>Tutorial Steps</h2>
      <ol>{steps_html}</ol>
      <p><strong>Check for understanding:</strong> {escaped_check}</p>
    </section>

    <section>
      <h2>Architecture Diagram</h2>
      <pre class="mermaid diagram">{escaped_mermaid}</pre>
    </section>

    <section>
      <h2>Sources</h2>
      <ul>{citations_html}</ul>
    </section>
  </main>
</body>
</html>
"""


class ArtifactCloudStorageUploader:
    """Uploads generated artifacts to Cloud Storage when configured."""

    def __init__(
        self,
        *,
        bucket_name: str,
        prefix: str = "tutorial-artifacts",
        project_id: str | None = None,
    ) -> None:
        self._bucket_name = bucket_name.strip()
        self._prefix = prefix.strip("/")
        self._project_id = project_id
        self._bucket: Any | None = None
        self._error: str | None = None
        self._initialize()

    def _initialize(self) -> None:
        if not self._bucket_name:
            self._error = "Missing bucket name."
            return
        try:
            from google.cloud import storage  # type: ignore

            client = storage.Client(project=self._project_id or None)
            self._bucket = client.bucket(self._bucket_name)
            self._error = None
        except Exception as exc:  # noqa: BLE001
            self._error = str(exc)
            self._bucket = None

    @property
    def ready(self) -> bool:
        return self._bucket is not None and self._error is None

    @property
    def error(self) -> str | None:
        return self._error

    def upload_file(
        self,
        *,
        artifact_id: str,
        source_path: Path,
        target_name: str,
        content_type: str,
    ) -> str | None:
        if not self.ready or self._bucket is None:
            return None
        object_path = "/".join(
            part for part in [self._prefix, artifact_id, target_name] if part
        )
        blob = self._bucket.blob(object_path)
        blob.upload_from_filename(str(source_path), content_type=content_type)
        return f"gs://{self._bucket_name}/{object_path}"


class TutorialArtifactManager:
    """Creates, stores, and reloads tutorial artifacts across service restarts."""

    def __init__(
        self,
        *,
        output_dir: Path,
        cloud_uploader: ArtifactCloudStorageUploader | None = None,
    ) -> None:
        self._output_dir = output_dir
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._metadata_name = "metadata.json"
        self._records: dict[str, TutorialArtifactRecord] = {}
        self._lock = threading.RLock()
        self._cloud_uploader = cloud_uploader
        self._load_existing_records()

    def _load_existing_records(self) -> None:
        loaded = 0
        with self._lock:
            for metadata_path in self._output_dir.glob(f"*/{self._metadata_name}"):
                try:
                    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                    artifact_payload = payload.get("artifact")
                    if not isinstance(artifact_payload, dict):
                        continue
                    response = TutorialArtifactCreateResponse(**artifact_payload)
                    artifact_dir = metadata_path.parent
                    html_path = self._resolve_existing_path(
                        response.html_path, artifact_dir / "tutorial.html"
                    )
                    if html_path is None:
                        continue
                    pdf_path = self._resolve_existing_path(
                        response.pdf_path, artifact_dir / "tutorial.pdf"
                    )
                    if response.html_path != str(html_path) or response.pdf_path != (
                        str(pdf_path) if pdf_path else None
                    ):
                        response = response.model_copy(
                            update={
                                "html_path": str(html_path),
                                "pdf_path": str(pdf_path) if pdf_path else None,
                            }
                        )
                    self._records[response.artifact_id] = TutorialArtifactRecord(
                        artifact_id=response.artifact_id,
                        created_at=response.created_at,
                        topic=response.topic,
                        html_path=html_path,
                        pdf_path=pdf_path,
                        response_payload=response,
                    )
                    loaded += 1
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning(
                        "Failed loading artifact metadata from %s: %s",
                        metadata_path,
                        exc,
                    )
        if loaded:
            LOGGER.info("Loaded %s persisted tutorial artifacts.", loaded)

    def _resolve_existing_path(
        self, raw_path: str | None, fallback: Path
    ) -> Path | None:
        if raw_path:
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = (fallback.parent / candidate).resolve()
            if candidate.exists():
                return candidate
        return fallback if fallback.exists() else None

    def _save_metadata_file(
        self,
        *,
        artifact_dir: Path,
        request: TutorialArtifactCreateRequest,
        response: TutorialArtifactCreateResponse,
    ) -> None:
        payload = {
            "saved_at": utc_now_iso(),
            "request": request.model_dump(mode="json"),
            "artifact": response.model_dump(mode="json"),
        }
        metadata_path = artifact_dir / self._metadata_name
        metadata_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def create_artifact(
        self, request: TutorialArtifactCreateRequest
    ) -> TutorialArtifactCreateResponse:
        created_at = utc_now_iso()
        user_lines = _parse_transcript_lines(request.user_transcript)
        agent_lines = _parse_transcript_lines(request.agent_transcript)
        topic = _select_topic(request.topic, request.citations, user_lines, agent_lines)
        category = _topic_category(topic)
        key_points = _build_key_points(topic, category, agent_lines, request.citations)
        tutorial_steps = _build_tutorial_steps(topic, category, request.citations)
        summary = _build_summary(topic, agent_lines, user_lines)
        check_for_understanding = _build_check_question(topic, category)
        mermaid_diagram = _build_mermaid(topic, category)

        hash_basis = "|".join(
            [request.user_id, request.session_id, topic, created_at]
        ).encode("utf-8")
        short_hash = hashlib.sha1(hash_basis).hexdigest()[:10]
        artifact_id = f"{_slugify(topic)}-{short_hash}"

        artifact_dir = self._output_dir / artifact_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        html_path = artifact_dir / "tutorial.html"
        html_path.write_text(
            _render_html_document(
                artifact_id=artifact_id,
                created_at=created_at,
                topic=topic,
                summary=summary,
                key_points=key_points,
                tutorial_steps=tutorial_steps,
                check_for_understanding=check_for_understanding,
                mermaid_diagram=mermaid_diagram,
                citations=request.citations,
            ),
            encoding="utf-8",
        )

        pdf_path: Path | None = None
        notes: list[str] = []
        if request.include_pdf:
            pdf_path = artifact_dir / "tutorial.pdf"
            try:
                from weasyprint import HTML  # type: ignore

                HTML(filename=str(html_path)).write_pdf(str(pdf_path))
                notes.append("PDF generated with WeasyPrint.")
            except Exception as exc:  # noqa: BLE001
                pdf_path = None
                notes.append(
                    "PDF generation was requested but unavailable. "
                    f"Reason: {exc}"
                )

        cloud_html_url: str | None = None
        cloud_pdf_url: str | None = None
        storage_provider: str | None = None
        if self._cloud_uploader is not None:
            if not self._cloud_uploader.ready:
                notes.append(
                    "Cloud Storage upload skipped. "
                    f"Reason: {self._cloud_uploader.error or 'uploader not ready'}"
                )
            else:
                try:
                    cloud_html_url = self._cloud_uploader.upload_file(
                        artifact_id=artifact_id,
                        source_path=html_path,
                        target_name="tutorial.html",
                        content_type="text/html; charset=utf-8",
                    )
                    if pdf_path is not None:
                        cloud_pdf_url = self._cloud_uploader.upload_file(
                            artifact_id=artifact_id,
                            source_path=pdf_path,
                            target_name="tutorial.pdf",
                            content_type="application/pdf",
                        )
                    if cloud_html_url:
                        notes.append(f"Uploaded HTML to Cloud Storage: {cloud_html_url}")
                    if cloud_pdf_url:
                        notes.append(f"Uploaded PDF to Cloud Storage: {cloud_pdf_url}")
                    storage_provider = "gcs" if cloud_html_url or cloud_pdf_url else None
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"Cloud Storage upload failed: {exc}")

        response = TutorialArtifactCreateResponse(
            artifact_id=artifact_id,
            created_at=created_at,
            topic=topic,
            summary=summary,
            key_points=key_points,
            tutorial_steps=tutorial_steps,
            check_for_understanding=check_for_understanding,
            mermaid_diagram=mermaid_diagram,
            citations=request.citations,
            html_path=str(html_path),
            html_url=f"/artifacts/{artifact_id}/html",
            pdf_path=str(pdf_path) if pdf_path else None,
            pdf_url=f"/artifacts/{artifact_id}/pdf" if pdf_path else None,
            cloud_html_url=cloud_html_url,
            cloud_pdf_url=cloud_pdf_url,
            storage_provider=storage_provider,
            notes=notes,
        )

        self._save_metadata_file(
            artifact_dir=artifact_dir,
            request=request,
            response=response,
        )

        with self._lock:
            self._records[artifact_id] = TutorialArtifactRecord(
                artifact_id=artifact_id,
                created_at=created_at,
                topic=topic,
                html_path=html_path,
                pdf_path=pdf_path,
                response_payload=response,
            )
        return response

    def get_html_path(self, artifact_id: str) -> Path | None:
        with self._lock:
            record = self._records.get(artifact_id)
            if record and record.html_path.exists():
                return record.html_path
        fallback = self._output_dir / artifact_id / "tutorial.html"
        return fallback if fallback.exists() else None

    def get_pdf_path(self, artifact_id: str) -> Path | None:
        with self._lock:
            record = self._records.get(artifact_id)
            if record and record.pdf_path and record.pdf_path.exists():
                return record.pdf_path
        fallback = self._output_dir / artifact_id / "tutorial.pdf"
        return fallback if fallback.exists() else None

    def list_recent(self, limit: int = 20) -> list[TutorialArtifactCreateResponse]:
        with self._lock:
            records = sorted(
                self._records.values(),
                key=lambda item: item.created_at,
                reverse=True,
            )
            return [item.response_payload for item in records[: max(1, min(200, limit))]]

    @property
    def output_dir(self) -> Path:
        return self._output_dir
