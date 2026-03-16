"""API schemas for Computer Use routes."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.app.computer_use.worker import (
    default_computer_use_model,
    default_computer_use_provider,
)


class ComputerUseRunRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    initial_url: str = Field(default="https://www.google.com", max_length=2000)
    max_steps: int = Field(default=30, ge=1, le=120)
    model: str = Field(default_factory=default_computer_use_model)
    provider: Literal["playwright", "browserbase"] = Field(
        default_factory=default_computer_use_provider
    )
    screen_width: int = Field(default=1440, ge=640, le=3840)
    screen_height: int = Field(default=900, ge=480, le=2160)
    excluded_actions: list[str] = Field(default_factory=list)


class ComputerUseSafetyResponseRequest(BaseModel):
    run_id: str = Field(min_length=8, max_length=128)
    confirmation_id: str = Field(min_length=8, max_length=128)
    acknowledged: bool
    keep_session_open: bool = False


class PendingSafetyConfirmationPayload(BaseModel):
    confirmation_id: str
    step_index: int
    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    decision: str
    explanation: str | None = None


class ComputerUseStep(BaseModel):
    index: int
    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    status: Literal["executed", "unsupported", "blocked_by_safety", "error"]
    url: str | None = None
    error: str | None = None
    safety_decision: str | None = None


class ComputerUseRunResponse(BaseModel):
    status: Literal[
        "completed",
        "max_steps_exceeded",
        "awaiting_confirmation",
        "safety_denied",
        "failed",
    ]
    model: str
    provider: Literal["playwright", "browserbase"]
    query: str
    run_id: str | None = None
    final_reasoning: str | None = None
    completed_steps: int
    max_steps: int
    debug_url: str | None = None
    steps: list[ComputerUseStep] = Field(default_factory=list)
    pending_confirmation: PendingSafetyConfirmationPayload | None = None
    started_at: str
    completed_at: str
    error: str | None = None


class ComputerUseHealthResponse(BaseModel):
    status: Literal["ready", "degraded"]
    provider_default: Literal["playwright", "browserbase"]
    providers: dict[str, dict[str, Any]]
    model_default: str
    active_runs: int
    notes: list[str] = Field(default_factory=list)
