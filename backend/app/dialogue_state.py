"""Session 03 dialogue state helpers for CloudTutor."""

from __future__ import annotations

import re
from dataclasses import dataclass


_MORE_DETAILS_PATTERNS = (
    "show me more",
    "more details",
    "go deeper",
    "dig deeper",
    "tell me more",
    "expand on",
    "elaborate",
    "walk me through",
    "open docs",
    "open documentation",
    "documentation please",
)

_QUESTION_PREFIXES = (
    "what",
    "why",
    "how",
    "when",
    "where",
    "who",
    "which",
    "can",
    "could",
    "would",
    "should",
    "is",
    "are",
    "do",
    "does",
    "did",
    "explain",
    "tell",
    "show",
    "compare",
)

_CLOUD_TOPIC_PATTERNS = (
    "google cloud",
    "gcp",
    "cloud run",
    "gke",
    "kubernetes engine",
    "app engine",
    "cloud functions",
    "cloud function",
    "cloud sql",
    "cloud storage",
    "cloud build",
    "cloud deploy",
    "cloud armor",
    "cloud load balancing",
    "cloud monitoring",
    "cloud logging",
    "cloud scheduler",
    "cloud tasks",
    "cloud dns",
    "cloud cdn",
    "cloud nat",
    "bigquery",
    "pub/sub",
    "pubsub",
    "vertex ai",
    "firebase",
    "firestore",
    "secret manager",
    "artifact registry",
    "dataflow",
    "dataproc",
    "spanner",
    "memorystore",
    "composer",
    "workflows",
    "eventarc",
    "api gateway",
    "service account",
    "iam",
    "vpc",
)

_SERVICE_TOPIC_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Firebase Cloud Functions",
        (
            "firebase cloud function",
            "firebase cloud functions",
            "firebase functions",
            "firebase function",
        ),
    ),
    (
        "Firebase Hosting",
        (
            "firebase hosting",
        ),
    ),
    (
        "Cloud Run",
        (
            "cloud run",
        ),
    ),
    (
        "Cloud Functions",
        (
            "google cloud functions",
            "google cloud function",
            "cloud functions",
            "cloud function",
        ),
    ),
    (
        "Google Kubernetes Engine",
        (
            "google kubernetes engine",
            "kubernetes engine",
            "gke",
        ),
    ),
    (
        "BigQuery",
        (
            "bigquery",
        ),
    ),
    (
        "Cloud SQL",
        (
            "cloud sql",
        ),
    ),
    (
        "Firestore",
        (
            "cloud firestore",
            "firestore",
        ),
    ),
    (
        "Cloud Storage",
        (
            "cloud storage",
            "gcs bucket",
            "gcs",
        ),
    ),
    (
        "Pub/Sub",
        (
            "pub/sub",
            "pubsub",
        ),
    ),
    (
        "Cloud Build",
        (
            "cloud build",
        ),
    ),
    (
        "Cloud Deploy",
        (
            "cloud deploy",
        ),
    ),
    (
        "Cloud Armor",
        (
            "cloud armor",
        ),
    ),
    (
        "Cloud Load Balancing",
        (
            "cloud load balancing",
            "load balancing",
        ),
    ),
    (
        "Vertex AI",
        (
            "vertex ai",
        ),
    ),
    (
        "App Engine",
        (
            "app engine",
        ),
    ),
    (
        "Secret Manager",
        (
            "secret manager",
        ),
    ),
    (
        "Artifact Registry",
        (
            "artifact registry",
        ),
    ),
    (
        "Cloud Scheduler",
        (
            "cloud scheduler",
        ),
    ),
    (
        "Cloud Tasks",
        (
            "cloud tasks",
        ),
    ),
    (
        "Cloud Logging",
        (
            "cloud logging",
            "google cloud logging",
        ),
    ),
    (
        "Cloud Monitoring",
        (
            "cloud monitoring",
            "google cloud monitoring",
            "stackdriver monitoring",
        ),
    ),
    (
        "Cloud DNS",
        (
            "cloud dns",
        ),
    ),
    (
        "Cloud CDN",
        (
            "cloud cdn",
        ),
    ),
    (
        "Cloud NAT",
        (
            "cloud nat",
        ),
    ),
    (
        "Spanner",
        (
            "cloud spanner",
            "spanner",
        ),
    ),
    (
        "Dataflow",
        (
            "dataflow",
            "cloud dataflow",
        ),
    ),
    (
        "Dataproc",
        (
            "dataproc",
            "cloud dataproc",
        ),
    ),
    (
        "Memorystore",
        (
            "memorystore",
            "cloud memorystore",
        ),
    ),
    (
        "Cloud Composer",
        (
            "cloud composer",
            "composer",
        ),
    ),
    (
        "Workflows",
        (
            "workflows",
            "cloud workflows",
        ),
    ),
    (
        "Eventarc",
        (
            "eventarc",
        ),
    ),
    (
        "API Gateway",
        (
            "api gateway",
            "cloud api gateway",
        ),
    ),
    (
        "Service Accounts",
        (
            "service account",
            "service accounts",
        ),
    ),
    (
        "IAM",
        (
            "identity and access management",
            " iam ",
            " iam",
            "iam ",
            " iam.",
            " iam?",
            " iam,",
        ),
    ),
    (
        "VPC",
        (
            "vpc",
            "virtual private cloud",
        ),
    ),
)

_COMPARE_MARKERS = (" vs ", " versus ", "compare ", "difference", "differences")

_GROUNDING_HINT_PATTERNS = (
    "latest",
    "current",
    "today",
    "new",
    "recent",
    "pricing",
    "price",
    "cost",
    "quota",
    "limit",
    "limits",
    "sla",
    "availability",
    "region",
    "regions",
    "deprecat",
    "release",
    "version",
    "ga",
    "preview",
    "beta",
    "announcement",
    "roadmap",
    "compare",
    "difference",
)

_AFFIRMATIVE_VALUES = {
    "yes",
    "y",
    "yeah",
    "yep",
    "yup",
    "yea",
    "ja",
    "sure",
    "ok",
    "okay",
    "please do",
    "go ahead",
    "go for it",
    "proceed",
    "lets do it",
    "let's do it",
    "sounds good",
    "that works",
    "works for me",
    "absolutely",
    "definitely",
    "affirmative",
    "do it",
    "continue",
}

_RESUME_VALUES = {
    "continue",
    "resume",
    "go on",
    "proceed",
    "carry on",
    "keep going",
}

_NEGATIVE_VALUES = {
    "no",
    "n",
    "nope",
    "nah",
    "nej",
    "not now",
    "later",
    "skip",
    "cancel",
    "stop",
    "no thanks",
    "not interested",
    "not really",
    "don't",
    "do not",
}


@dataclass
class TutorDialogueState:
    """Tracks session-level tutor flow state for deterministic branching."""

    current_topic: str | None = None
    topic_locked: bool = False
    locked_topic: str | None = None
    locked_docs_url: str | None = None
    last_answer: str | None = None
    branch_context: str | None = None
    awaiting_doc_confirmation: bool = False
    last_intent: str = "unknown"
    guided_use_case_mode: bool = False
    guided_use_case_ready: bool = False
    guided_use_case_index: int = 0
    guided_use_case_topic: str | None = None
    guided_use_case_url: str | None = None
    guided_use_case_summary: str | None = None

    def snapshot(self) -> dict[str, str | bool | int]:
        return {
            "current_topic": self.current_topic or "",
            "topic_locked": self.topic_locked,
            "locked_topic": self.locked_topic or "",
            "locked_docs_url": self.locked_docs_url or "",
            "last_answer": self.last_answer or "",
            "branch_context": self.branch_context or "",
            "awaiting_doc_confirmation": self.awaiting_doc_confirmation,
            "last_intent": self.last_intent,
            "guided_use_case_mode": self.guided_use_case_mode,
            "guided_use_case_ready": self.guided_use_case_ready,
            "guided_use_case_index": self.guided_use_case_index,
            "guided_use_case_topic": self.guided_use_case_topic or "",
            "guided_use_case_url": self.guided_use_case_url or "",
            "guided_use_case_summary": self.guided_use_case_summary or "",
        }


def normalize_text(text: str) -> str:
    return " ".join(text.strip().lower().split())


def is_affirmative(text: str) -> bool:
    normalized = normalize_text(text)
    if normalized in _AFFIRMATIVE_VALUES:
        return True

    if any(
        phrase in normalized
        for phrase in (
            "go ahead",
            "go for it",
            "proceed",
            "let's do it",
            "lets do it",
            "sounds good",
            "works for me",
            "show me more",
        )
    ):
        return True

    return bool(
        re.search(
            r"\b(yes|yeah|yep|yup|sure|okay|ok|absolutely|definitely|affirmative)\b",
            normalized,
        )
    )


def is_negative(text: str) -> bool:
    normalized = normalize_text(text)
    if normalized in _NEGATIVE_VALUES:
        return True

    if any(
        phrase in normalized
        for phrase in (
            "no thanks",
            "not now",
            "not interested",
            "maybe later",
            "don't",
            "do not",
        )
    ):
        return True

    return bool(re.search(r"\b(no|nope|nah|negative)\b", normalized))


def is_resume_request(text: str) -> bool:
    normalized = normalize_text(text)
    return normalized in _RESUME_VALUES


def is_next_use_case_request(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    next_markers = (
        "next",
        "next one",
        "next use case",
        "move on",
        "move to next",
        "continue",
        "go on",
        "another use case",
        "show another",
    )
    return any(marker in normalized for marker in next_markers)


def is_stop_use_case_request(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    stop_markers = (
        "stop",
        "pause this",
        "that's enough",
        "thats enough",
        "no more",
        "done for now",
        "skip this",
        "not now",
    )
    return any(marker in normalized for marker in stop_markers)


def is_more_details_request(text: str) -> bool:
    normalized = normalize_text(text)
    return any(pattern in normalized for pattern in _MORE_DETAILS_PATTERNS)


def looks_like_question(text: str) -> bool:
    normalized = normalize_text(text)
    if "?" in text:
        return True
    return normalized.startswith(_QUESTION_PREFIXES)


def _extract_service_topics(normalized_text: str) -> list[str]:
    found: list[str] = []
    padded = f" {normalized_text} "
    for canonical, patterns in _SERVICE_TOPIC_RULES:
        if any(pattern in padded for pattern in patterns):
            found.append(canonical)
    unique: list[str] = []
    for topic in found:
        if topic not in unique:
            unique.append(topic)
    return unique


def infer_cloud_service_topic(text: str) -> str | None:
    normalized = normalize_text(text)
    if not normalized:
        return None

    service_topics = _extract_service_topics(normalized)
    if not service_topics:
        return None

    if len(service_topics) >= 2 and any(
        marker in f" {normalized} " for marker in _COMPARE_MARKERS
    ):
        return f"{service_topics[0]} vs {service_topics[1]}"

    return service_topics[0]


def infer_topic(text: str, previous_topic: str | None) -> str | None:
    normalized = normalize_text(text)
    if not normalized:
        return previous_topic

    if (
        is_affirmative(text)
        or is_negative(text)
        or is_resume_request(text)
        or is_more_details_request(text)
    ) and not previous_topic and len(normalized.split()) <= 5:
        return None

    if (
        is_more_details_request(text)
        or is_affirmative(text)
        or is_negative(text)
        or is_resume_request(text)
    ) and previous_topic:
        return previous_topic

    service_topic = infer_cloud_service_topic(text)
    if service_topic:
        return service_topic
    return previous_topic


def is_end_conversation_request(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    patterns = {
        "goodbye", "bye", "end conversation", "stop conversation", "that is all", 
        "that's all", "im good", "i am good", "thanks bye", "thank you bye"
    }
    return any(p in normalized for p in patterns)


def detect_intent(text: str, *, awaiting_confirmation: bool) -> str:
    normalized = normalize_text(text)
    if not normalized:
        return "ambiguous"

    if is_end_conversation_request(normalized):
        return "end_conversation"

    if awaiting_confirmation:
        if is_negative(normalized):
            return "confirm_no"
        if is_affirmative(normalized) or is_more_details_request(normalized):
            return "confirm_yes"
        if looks_like_question(normalized):
            return "direct_question"
        if len(normalized.split()) > 4:
            # If they say a full sentence, it's not a simple yes/no response to confirmation.
            # Treat it as a completely new query and drop out of confirmation gracefully.
            return "ambiguous"
        return "confirm_unclear"

    if is_more_details_request(normalized):
        return "request_more_details"

    if looks_like_question(normalized):
        return "direct_question"

    # Keep flow moving for natural commands/statements, but still leave
    # one branch for short unclear utterances.
    if len(normalized.split()) <= 2:
        return "ambiguous"

    return "direct_question"


def should_ground_query(text: str, previous_topic: str | None = None) -> bool:
    """Returns True for cloud/factual prompts that should use fresh grounding."""
    normalized = normalize_text(text)
    if not normalized:
        return False

    topic_space = f"{normalized} {normalize_text(previous_topic or '')}".strip()
    has_cloud_topic = any(pattern in topic_space for pattern in _CLOUD_TOPIC_PATTERNS)
    has_grounding_hint = any(pattern in normalized for pattern in _GROUNDING_HINT_PATTERNS)

    # Strongly ground explicit requests for sources even if the cloud keyword is omitted.
    asks_for_sources = any(token in normalized for token in ("source", "sources", "reference", "references", "citation", "citations"))

    if asks_for_sources:
        return True
    if has_cloud_topic and has_grounding_hint:
        return True
    if has_cloud_topic and looks_like_question(text):
        return True
    return False
