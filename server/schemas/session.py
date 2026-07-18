"""
Pydantic schemas for session and chat API endpoints.

Each model includes field-level documentation for OpenAPI schema generation.
FastAPI uses these models to validate request bodies and serialise responses,
and they appear in the auto-generated /docs (Swagger UI).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ── Create Session ──────────────────────────────────────────────────────────


class CreateSessionRequest(BaseModel):
    """Request body for ``POST /api/sessions``.

    Creates a new root session for agent execution.
    """

    agent_name: str = Field(
        default="build",
        description="Agent definition name (e.g. 'build', 'plan'). "
        "Must be a known agent in the project's registry.",
    )
    repo_path: str = Field(
        description="Absolute path to the repository the agent will work on.",
    )
    title: str = Field(
        default="",
        description="Optional human-readable session title. "
        "Auto-generated if empty.",
    )


class CreateSessionResponse(BaseModel):
    """Response for ``POST /api/sessions``."""

    session_id: str = Field(
        description="12-character hex session identifier.",
    )
    agent_name: str = Field(description="Agent definition used for this session.")
    status: str = Field(description="Initial session status (always 'queued').")
    repo_path: str = Field(description="Repository path the session is scoped to.")
    created_at: str = Field(description="ISO-8601 creation timestamp.")


# ── Session Summary / Detail ────────────────────────────────────────────────


class SessionSummary(BaseModel):
    """Lightweight session representation for list views."""

    id: str = Field(description="12-char hex session identifier.")
    agent_name: str = Field(description="Agent definition name.")
    title: str = Field(description="Human-readable session title.")
    status: str = Field(
        description="Session status: 'queued' | 'running' | 'completed' | "
        "'failed' | 'cancelled'.",
    )
    mode: str = Field(description="Session mode: 'primary' | 'subagent'.")
    summary: str = Field(description="Result summary text (empty if not completed).")
    error: str = Field(description="Error message (empty if successful).")
    parent_id: str | None = Field(default=None, description="Parent session ID if this is a child/subagent.")
    created_at: str = Field(description="ISO-8601 creation timestamp.")
    updated_at: str = Field(description="ISO-8601 last-update timestamp.")
    completed_at: str | None = Field(default=None, description="ISO-8601 completion timestamp.")


class SessionDetail(BaseModel):
    """Full session representation for detail views."""

    id: str = Field(description="12-char hex session identifier.")
    parent_id: str | None = Field(default=None, description="Parent session ID.")
    root_id: str | None = Field(default=None, description="Root session ID (same as id for root sessions).")
    agent_name: str = Field(description="Agent definition name.")
    title: str = Field(description="Session title.")
    status: str = Field(description="Session status.")
    mode: str = Field(description="Session mode.")
    summary: str = Field(description="Result summary.")
    error: str = Field(description="Error message.")
    agent_kind: str = Field(description="'primary' | 'named_subagent' | 'fork'.")
    context_origin: str = Field(description="'fresh' | 'resumed' | 'parent_snapshot'.")
    execution_placement: str = Field(description="'foreground' | 'background'.")
    workspace_mode: str = Field(description="'current' | 'worktree'.")
    agent_depth: int = Field(description="Depth in the session tree (0 = root).")
    generation: int = Field(description="Run generation (increments on resume).")
    created_at: str = Field(description="ISO-8601 creation timestamp.")
    updated_at: str = Field(description="ISO-8601 last-update timestamp.")
    completed_at: str | None = Field(default=None, description="ISO-8601 completion timestamp.")
    metadata: dict = Field(default_factory=dict, description="Session metadata dict.")


# ── Messages ────────────────────────────────────────────────────────────────


class MessageResponse(BaseModel):
    """A single conversation message."""

    role: str = Field(description="Message role: 'user' | 'assistant' | 'tool'.")
    content: str = Field(description="Message text content (markdown for assistant).")
    tool_calls: list[dict] | None = Field(
        default=None,
        description="Tool invocations (present on assistant messages with tool use). "
        "Each item: ``{'name': str, 'params': {...}, 'id': str | None}``.",
    )
    tool_call_id: str | None = Field(
        default=None,
        description="Tool call ID (present on tool result messages).",
    )


# ── Chat ────────────────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    """Request body for ``POST /api/sessions/{id}/chat``.

    Executes one round of the ReAct agent loop.
    """

    prompt: str = Field(
        min_length=1,
        description="User's task description for the agent. "
        "This is the main input that drives the ReAct loop.",
    )
    agent_name: str | None = Field(
        default=None,
        description="Override the agent definition for this execution. "
        "If null, uses the session's default agent. "
        "Example values: 'build', 'plan', 'explore'.",
    )
    intent: str | None = Field(
        default=None,
        description="Task intent override. "
        "'edit' = write-capable mode, 'analysis' = read-only mode. "
        "If null, auto-detected from the agent definition.",
    )


class ChatResponse(BaseModel):
    """Response for ``POST /api/sessions/{id}/chat``.

    Contains the final result of one ReAct loop execution.
    """

    session_id: str = Field(
        description="The session ID that was executed.",
    )
    status: str = Field(
        description="Execution status: "
        "'success' | 'failed' | 'max_steps' | 'gave_up' | 'blocked' | 'cancelled'.",
    )
    summary: str = Field(
        description="Agent's final summary of what was accomplished.",
    )
    steps_taken: int = Field(
        description="Number of ReAct steps (LLM calls) executed.",
    )
    total_tokens: int = Field(
        description="Total tokens consumed across all LLM calls in this execution.",
    )
    error: str | None = Field(
        default=None,
        description="Error message if status is 'failed', 'blocked', or 'cancelled'.",
    )
    termination_reason: str | None = Field(
        default=None,
        description="Typed reason why execution stopped: "
        "'agent_gave_up' | 'max_steps' | 'budget_exhausted' | "
        "'user_cancelled' | 'model_error' | etc.",
    )


# ── Cancel ──────────────────────────────────────────────────────────────────


class CancelRequest(BaseModel):
    """Request body for ``POST /api/sessions/{id}/cancel``."""

    detail: str = Field(
        default="",
        description="Optional human-readable reason for cancellation.",
    )


class CancelResponse(BaseModel):
    """Response for ``POST /api/sessions/{id}/cancel``."""

    cancelled: bool = Field(
        description="True if an active session was found and cancellation "
        "signal was sent. False if the session had no active token "
        "(already completed or not found).",
    )


# ── Approval (placeholder for future use) ───────────────────────────────────


class ApproveRequest(BaseModel):
    """Request body for ``POST /api/sessions/{id}/approve``."""

    comment: str = Field(
        default="",
        description="Optional approval comment.",
    )


class RejectRequest(BaseModel):
    """Request body for ``POST /api/sessions/{id}/reject``."""

    reason: str = Field(
        min_length=1,
        description="Reason for rejection.",
    )


class ApprovalResponse(BaseModel):
    """Response for approval/rejection endpoints."""

    approved: bool = Field(description="True if approved, False if rejected.")
    session_id: str = Field(description="The session ID.")
    status: str = Field(description="Updated session status after the action.")
