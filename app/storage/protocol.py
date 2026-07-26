"""Storage backend protocol — abstract interface for session persistence.

All storage backends (SQLite, Redis, etc.) implement this protocol.
Business logic depends only on this interface, never on concrete backends.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from agent.session.models import (
    AgentCompletionNotification,
    AgentKind,
    AgentRunResult,
    SessionMode,
    SessionRecord,
    SessionStatus,
)
from llm.base import LLMMessage


# ── Admin types ──────────────────────────────────────────────────────────────


@dataclass
class StorageStats:
    """Storage backend statistics, displayable in frontend."""

    backend: str = "unknown"  # "sqlite" | "redis"
    total_sessions: int = 0
    total_messages: int = 0
    db_size_bytes: int | None = None
    uptime_seconds: float = 0.0


# ── Protocol ─────────────────────────────────────────────────────────────────


@runtime_checkable
class StorageBackend(Protocol):
    """Session storage abstraction.

    All methods that accept a ``session_id`` which does not exist raise
    ``ValueError`` with the message "Unknown session: {session_id}".
    """

    # ── Session CRUD ──────────────────────────────────────────────────────

    def create_session(
        self,
        *,
        agent_name: str,
        mode: SessionMode,
        repo_path: str,
        title: str,
        agent_kind: AgentKind = AgentKind.PRIMARY,
        parent_id: str | None = None,
        root_id: str | None = None,
        metadata: dict | None = None,
    ) -> SessionRecord:
        """Create a new session with status QUEUED.

        Raises ``ValueError`` on invalid parameters (e.g. unknown parent).
        """
        ...

    def get_session(self, session_id: str) -> SessionRecord | None:
        """Get a single session by ID. Returns None if not found."""
        ...

    def list_sessions(
        self, limit: int = 50, offset: int = 0,
    ) -> list[SessionRecord]:
        """List all sessions ordered by ``updated_at DESC``."""
        ...

    def update_status(
        self, session_id: str, status: SessionStatus, error: str = "",
    ) -> None:
        """Update session status and updated_at timestamp. Raises ValueError if not found."""
        ...

    def set_summary(
        self, session_id: str, summary: str, *, status: SessionStatus,
    ) -> None:
        """Set session summary, status, and completed_at. Raises ValueError if not found."""
        ...

    # ── Run outcomes ─────────────────────────────────────────────────────

    def list_runs(self, session_id: str, *, limit: int = 20) -> list[dict]:
        """List structured run outcomes for a session, newest first."""
        ...

    def delete_session(self, session_id: str) -> bool:
        """Permanently delete a session and all its messages. Returns True if deleted."""
        ...

    def delete_sessions_batch(self, session_ids: list[str]) -> int:
        """Delete multiple sessions in one transaction.

        Returns the count of sessions actually deleted (IDs that existed).
        Non-existent IDs are silently skipped.
        """
        ...

    def update_title(self, session_id: str, title: str) -> bool:
        """Update a session's title. Returns True if updated, False if not found."""
        ...

    def update_agent_name(self, session_id: str, agent_name: str) -> bool:
        """Update a session's agent_name. Returns True if updated, False if not found."""
        ...

    def update_metadata(self, session_id: str, metadata: dict) -> bool:
        """Replace session metadata. Returns True if updated, False if not found."""
        ...

    # ── Messages ──────────────────────────────────────────────────────────

    def append_message(
        self, session_id: str, message: LLMMessage,
    ) -> None:
        """Append a message to a session's conversation history.

        Raises ``ValueError`` if the session does not exist.
        """
        ...

    def list_messages(self, session_id: str) -> list[LLMMessage]:
        """List all messages in a session, ordered by creation time (oldest first).

        Raises ``ValueError`` if the session does not exist.
        Returns an empty list if the session has no messages.
        """
        ...

    def count_messages(self, session_id: str) -> int:
        """Count messages in a session. Returns 0 if session not found."""
        ...

    # ── Child / fork sessions ────────────────────────────────────────────

    def list_child_sessions(self, parent_id: str) -> list[SessionRecord]:
        """List direct child sessions of a parent, ordered by creation time."""
        ...

    # ── Agent notifications (child results) ──────────────────────────────

    def append_notification(
        self, notification: AgentCompletionNotification,
    ) -> None:
        """Persist a child completion notification for the parent to consume."""
        ...

    def claim_pending_notifications(
        self, parent_session_id: str,
    ) -> tuple[AgentCompletionNotification, ...]:
        """Atomically claim all pending child results for one parent session."""
        ...

    # ── Session resume ────────────────────────────────────────────────────

    def prepare_resume(
        self, session_id: str, message: LLMMessage,
    ) -> SessionRecord:
        """Atomically append a prompt and begin a terminal child's next generation.

        Raises ``ValueError`` if the session is not terminal or not a subagent.
        """
        ...

    # ── Agent result ──────────────────────────────────────────────────────

    def set_agent_result(
        self, session_id: str, result: AgentRunResult,
    ) -> None:
        """Persist the typed child result after a session completes."""
        ...

    # ── Execution stats ──────────────────────────────────────────────────

    def upsert_session_stats(
        self, session_id: str, *, agent_name: str, total_steps: int,
        total_tokens: int, total_duration_ms: int, status: str,
        tool_summary: str,
    ) -> None:
        """Insert or update aggregate stats for a completed session."""
        ...

    def insert_step_log(
        self, session_id: str, *, step_number: int, tool_name: str,
        tool_params: str, status: str, duration_ms: int, tokens: int,
        timestamp: str,
    ) -> None:
        """Record one step execution."""
        ...

    def insert_session_diff(
        self, session_id: str, *, step_number: int, file_path: str,
        diff_content: str,
    ) -> int:
        """Record a file diff from an Edit/Write operation. Returns row id."""
        ...

    def get_session_diffs(
        self, session_id: str, status: str | None = None,
    ) -> list[dict]:
        """List diffs for a session, optionally filtered by status."""
        ...

    def update_diff_status(
        self, diff_id: int, status: str, comment: str = "",
    ) -> bool:
        """Approve or reject a diff (pending → approved/rejected)."""
        ...

    def upsert_daily_rollup(
        self, date: str, *, session_count: int, total_tokens: int,
        total_duration_ms: int, tool_summary: str, status_summary: str,
    ) -> None:
        """Insert or update daily aggregate."""
        ...

    def get_daily_rollups(self, days: int = 30) -> list[dict]:
        """Get daily rollups for the last N days."""
        ...

    def get_session_stats(self, session_id: str) -> dict | None:
        """Get aggregate stats for one session."""
        ...

    def get_session_steps(self, session_id: str) -> list[dict]:
        """Get per-step logs for one session."""
        ...

    # ── Typed trace events ────────────────────────────────────────────────

    def insert_trace_event(
        self,
        session_id: str,
        event: dict,
        *,
        source: str = "event_bus",
    ) -> dict:
        """Persist one typed WebSocket event and return it with backend sequence metadata."""
        ...

    def list_trace_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        limit: int = 200,
    ) -> list[dict]:
        """List typed WebSocket events for a session after the given sequence."""
        ...

    # ── Storage admin ─────────────────────────────────────────────────────

    def get_stats(self) -> StorageStats:
        """Return storage backend statistics."""
        ...

    def ping(self) -> bool:
        """Health check. Returns True if the backend is responsive."""
        ...

    def close(self) -> None:
        """Release backend resources (connections, file handles)."""
        ...
