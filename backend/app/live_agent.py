"""CloudTutor live agent definition for Session 03."""

from __future__ import annotations

import os
import re

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.llm_agent import Agent
from google.adk.models import LlmResponse
from google.adk.tools import google_search
from google.genai import types


def _use_vertex() -> bool:
    value = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "0").strip().lower()
    return value in {"1", "true", "yes"}


def _default_live_model() -> str:
    if _use_vertex():
        return "gemini-live-2.5-flash-native-audio"
    return "gemini-2.5-flash-native-audio-preview-12-2025"


LIVE_MODEL = os.getenv("CLOUDTUTOR_LIVE_MODEL", _default_live_model())

AGENT_INSTRUCTION = """
You are CloudTutor, a wildly enthusiastic and supportive live cloud tutor.

Behavior rules:
- Be extraordinarily enthusiastic and warm in every response! Sound genuinely excited.
- Keep spoken answers concise and clear (2-5 short sentences). Give the user the "Gist" of the concept, but ALWAYS vary your phrasing (e.g. "Okay, okay, this is the Gist of it...", "In a nutshell, here is how it works...", "Here is the quick breakdown...", etc.). Do not repeat the exact same transition.
- For direct questions about a cloud service or concept: answer first, then aggressively and enthusiastically offer to navigate them to the documentation.
- After explaining a product, always invite the user to continue. Vary your phrasing for this invite (e.g. "If you want more, I can totally help by navigating you straight to the docs! Just say 'show me more'!", or "I can pull up the official docs for you right now if you want to see them. Say yes!").
- Treat "cloud function" and "cloud functions" as Google Cloud Functions (2nd gen context by default).
- If the user requests "more details", enthusiastically ask for explicit yes/no confirmation before any documentation exploration.
- When the user says "show me more" or asks for docs, explicitly confirm you are taking them there. Vary your phrasing (e.g. "Awesome! Let's do this, I am navigating you to the documentation now!", or "You got it! Pulling up the docs for you instantly!").
- Never begin doc-exploration actions without explicit user confirmation in the conversation.
- If the user asks for current/factual cloud details, use google_search.
- Prefer official sources when possible.
- If uncertain, admit it cheerfully!
""".strip()

_BROWSER_INVITE = (
    'Want to see the official docs? Just say "show me more" or "yes" and I will navigate you there immediately!'
)
_DOC_CONFIRMATION_FOLLOWUP = (
    "Awesome! Let's do this. I'm pulling up the official docs for you in the browser right now!"
)
_GENERIC_FOLLOWUP_PATTERN = re.compile(
    r"(?is)\s*(?:are you|would you|do you want|which|what kind of|interested in|"
    r"would you like)[^.?!]*\?\s*$"
)
_QUESTION_LIKE_SUFFIXES = (
    "specific project?",
    "specific feature?",
    "event triggers?",
    "custom domains?",
    "other tools?",
)


def _coalesce_text_parts(llm_response: LlmResponse) -> str:
    if not llm_response.content or not llm_response.content.parts:
        return ""
    return "\n".join(part.text for part in llm_response.content.parts if part.text).strip()


def _split_sources(text: str) -> tuple[str, str]:
    marker = "\n\nSources:\n"
    if marker in text:
        body, sources = text.split(marker, 1)
        return body.strip(), "Sources:\n" + sources.strip()
    if "\nSources:\n" in text:
        body, sources = text.split("\nSources:\n", 1)
        return body.strip(), "Sources:\n" + sources.strip()
    return text.strip(), ""


def _looks_like_doc_confirmation(body_text: str) -> bool:
    lowered = body_text.lower()
    return (
        "should i continue" in lowered
        or ("open the official docs" in lowered and "browser" in lowered)
        or ("show me more" in lowered and "browser" in lowered)
    )


def _looks_like_more_details_deflection(body_text: str) -> bool:
    lowered = body_text.lower()
    markers = (
        "since you'd like to see more",
        "since you would like to see more",
        "would you prefer to explore",
        "quickstart guides",
        "specific features",
        "integrates with other tools",
        "learn about specific features",
    )
    return any(marker in lowered for marker in markers)


def _should_offer_browser_followup(body_text: str) -> bool:
    lowered = body_text.lower()
    if not body_text:
        return False
    if "sources:" in lowered:
        return False
    if _looks_like_doc_confirmation(body_text):
        return False
    if "browser" in lowered or "official docs" in lowered or "show me more" in lowered:
        return False
    cloud_markers = (
        "google cloud",
        "cloud run",
        "cloud functions",
        "cloud function",
        "cloud sql",
        "cloud storage",
        "firebase",
        "vertex ai",
        "gke",
        "bigquery",
        "serverless",
        "containerized",
        "http trigger",
    )
    return any(marker in lowered for marker in cloud_markers)


def _normalize_browser_followup(body_text: str) -> str:
    trimmed = body_text.strip()
    if not trimmed:
        return trimmed

    trimmed = _GENERIC_FOLLOWUP_PATTERN.sub("", trimmed).strip()
    lowered = trimmed.lower()
    for suffix in _QUESTION_LIKE_SUFFIXES:
        if lowered.endswith(suffix):
            trimmed = trimmed[: -len(suffix)].rstrip(" ,")
            break

    if trimmed.endswith("?"):
        pieces = re.split(r"(?<=[.?!])\s+", trimmed)
        if len(pieces) > 1:
            trimmed = " ".join(pieces[:-1]).strip()

    trimmed = trimmed.rstrip()
    if trimmed and trimmed[-1] not in ".!?":
        trimmed += "."
    merged = f"{trimmed} {_BROWSER_INVITE}".strip()
    merged = re.sub(
        r'(?i)\bjust\s+sometimes\s+say\s+"show me more"\.?',
        'Just say "show me more."',
        merged,
    )
    return merged


def _append_grounding_references(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> LlmResponse:
    """Appends grounding links when search grounding metadata is present."""
    del callback_context
    if not llm_response.content or not llm_response.content.parts:
        return llm_response
    if not llm_response.grounding_metadata:
        return llm_response

    references: list[str] = []
    for chunk in llm_response.grounding_metadata.grounding_chunks or []:
        title = ""
        uri = ""
        if chunk.retrieved_context:
            title = chunk.retrieved_context.title or ""
            uri = chunk.retrieved_context.uri or ""
        elif chunk.web:
            title = chunk.web.title or ""
            uri = chunk.web.uri or ""
        if not uri:
            continue
        label = title.strip() or uri
        references.append(f"- [{label}]({uri})")

    if not references:
        return llm_response

    body_parts = [part.text for part in llm_response.content.parts if part.text]
    body_text = "\n".join(body_parts).strip()

    if "Sources:" in body_text:
        return llm_response

    source_block = "Sources:\n" + "\n".join(references[:4])
    merged_text = f"{body_text}\n\n{source_block}" if body_text else source_block
    llm_response.content.parts = [types.Part(text=merged_text)]
    return llm_response


def _enforce_browser_guided_followup(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> LlmResponse:
    del callback_context
    body_text = _coalesce_text_parts(llm_response)
    if not body_text:
        return llm_response

    body, sources = _split_sources(body_text)
    if not body:
        return llm_response

    if _looks_like_doc_confirmation(body) or _looks_like_more_details_deflection(body):
        normalized = _DOC_CONFIRMATION_FOLLOWUP
    elif _should_offer_browser_followup(body):
        normalized = _normalize_browser_followup(body)
    else:
        return llm_response

    merged_text = normalized if not sources else f"{normalized}\n\n{sources}"
    llm_response.content.parts = [types.Part(text=merged_text)]
    return llm_response


def _postprocess_live_response(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> LlmResponse:
    llm_response = _append_grounding_references(callback_context, llm_response)
    return _enforce_browser_guided_followup(callback_context, llm_response)


root_agent = Agent(
    name="cloudtutor_live_agent",
    model=LIVE_MODEL,
    description="Realtime CloudTutor agent for Session 03 streaming and flow behavior.",
    instruction=AGENT_INSTRUCTION,
    tools=[google_search],
    after_model_callback=_postprocess_live_response,
)
