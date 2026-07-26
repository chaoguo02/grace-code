"""Typed API contracts for multi-agent code review."""

from __future__ import annotations

from pydantic import BaseModel, Field


class StartReviewRequest(BaseModel):
    """Start a read-only multi-agent review for one primary session."""

    focus: str = Field(
        default="",
        max_length=2000,
        description="Optional user focus applied to every reviewer task.",
    )
    max_agents: int = Field(
        default=3,
        ge=1,
        le=3,
        description="Maximum parallel read-only reviewers.",
    )


class ReviewTaskAttemptResponse(BaseModel):
    id: str
    attempt_number: int
    status: str
    child_session_id: str = ""
    result: dict = Field(default_factory=dict)
    error: str = ""
    started_at: str
    completed_at: str | None = None


class ReviewTaskResponse(BaseModel):
    id: str
    lens: str
    title: str
    status: str
    child_session_id: str = ""
    result: dict = Field(default_factory=dict)
    error: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    attempts: list[ReviewTaskAttemptResponse] = Field(default_factory=list)


class ReviewJobResponse(BaseModel):
    id: str
    session_id: str
    status: str
    workspace_revision: str
    head_commit: str
    retry_of: str = ""
    snapshot_available: bool = False
    diff_hash: str
    changed_files: list[str] = Field(default_factory=list)
    focus: str = ""
    result: dict = Field(default_factory=dict)
    error: str = ""
    tasks: list[ReviewTaskResponse] = Field(default_factory=list)
    created_at: str
    updated_at: str
    completed_at: str | None = None
