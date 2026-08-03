"""
Pydantic schemas for execution stats, diffs, and review endpoints.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ── Session stats ─────────────────────────────────────────────────────────


class SessionStatsResponse(BaseModel):
    """Aggregate stats for one completed session."""

    session_id: str = Field(description="Session ID.")
    agent_name: str = Field(description="Agent used.")
    total_steps: int = Field(description="Total ReAct steps.")
    total_tokens: int = Field(description="Total tokens consumed.")
    total_duration_ms: int = Field(description="Total execution time in ms.")
    status: str = Field(description="Final status.")
    tool_summary: dict = Field(description="Per-tool call counts.")
    created_at: str = Field(description="ISO-8601 timestamp.")


class StepLogResponse(BaseModel):
    """One step in a session's execution log."""

    id: int = Field(description="Row ID.")
    session_id: str = Field(description="Session ID.")
    step_number: int = Field(description="Step number.")
    tool_name: str = Field(description="Tool invoked.")
    tool_params: str = Field(description="JSON tool parameters.")
    status: str = Field(description="success/error/denied.")
    duration_ms: int = Field(description="Step duration in ms.")
    tokens: int = Field(description="Tokens used in this step.")
    timestamp: str = Field(description="ISO-8601 timestamp.")


# ── Daily rollup ──────────────────────────────────────────────────────────


class DailyRollupResponse(BaseModel):
    """Aggregate stats for one day."""

    date: str = Field(description="YYYY-MM-DD.")
    session_count: int = Field(description="Sessions executed.")
    total_tokens: int = Field(description="Total tokens.")
    total_duration_ms: int = Field(description="Total duration in ms.")
    tool_summary: dict = Field(description="Per-tool counts.")
    status_summary: dict = Field(description="Per-status counts.")


# ── Diffs / Review ────────────────────────────────────────────────────────


class SessionDiffResponse(BaseModel):
    """One file diff from a session."""

    id: int = Field(description="Diff row ID.")
    session_id: str = Field(description="Session ID.")
    step_number: int = Field(description="Step where change was made.")
    file_path: str = Field(description="Modified file path.")
    diff_content: str = Field(description="Unified diff text.")
    status: str = Field(description="pending/approved/rejected.")
    review_comment: str = Field(description="Review feedback.")
    created_at: str = Field(description="ISO-8601 timestamp.")


class UpdateDiffRequest(BaseModel):
    """Request body for ``PATCH /api/diffs/{id}``."""

    status: str = Field(description="'approved' or 'rejected'.")
    comment: str = Field(default="", description="Optional review comment.")
