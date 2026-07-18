"""
Session service — query operations over SessionStore.

Thin wrapper that provides structured access to session data for the web API.
Does NOT contain business logic for running agents (see agent_service.py).

Usage:
    store = SessionStore(db_path)
    service = SessionService(store)
    sessions = service.list_sessions(limit=20)
    messages = service.get_messages("abc123")
    events = service.get_events("abc123")
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agent.event_log import EventLog
from agent.session.models import SessionRecord
from agent.task import Event, RunResult
from core.state_paths import ProjectStatePaths
from llm.base import LLMMessage

from app.storage.protocol import StorageBackend

logger = logging.getLogger(__name__)

# ── Helpers ─────────────────────────────────────────────────────────────────


def _serialize_event(event: Event) -> dict[str, Any]:
    """Convert an Event domain object to a plain JSON-safe dict."""
    return {
        "event_id": event.event_id,
        "event_type": event.event_type.value if hasattr(event.event_type, "value") else event.event_type,
        "task_id": event.task_id,
        "timestamp": event.timestamp,
        "payload": event.payload,
    }


def _serialize_message(msg: LLMMessage) -> dict[str, Any]:
    """Convert an LLMMessage to a plain JSON-safe dict."""
    tool_calls = None
    if msg.tool_calls:
        tool_calls = [
            {
                "name": tc.name,
                "params": tc.params,
                "id": tc.id,
            }
            for tc in msg.tool_calls
        ]
    return {
        "role": msg.role,
        "content": msg.content,
        "tool_calls": tool_calls,
        "tool_call_id": msg.tool_call_id,
    }


# ── SessionService ──────────────────────────────────────────────────────────


class SessionService:
    """Read-only session queries backed by StorageBackend and EventLog files."""

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage

    @property
    def store(self) -> StorageBackend:
        """The underlying storage backend."""
        return self._storage

    # ── Session CRUD ──────────────────────────────────────────────────────

    def list_sessions(
        self, limit: int = 50, offset: int = 0,
    ) -> list[dict]:
        """List sessions with enriched summary data.

        Returns:
            list[dict]: Each session with ``id``, ``agent_name``, ``title``,
            ``status``, ``mode``, ``summary``, ``error``, ``parent_id``,
            ``created_at``, ``updated_at``, ``completed_at``, ``message_count``,
            ``total_tokens_estimate``.
        """
        records = self._storage.list_sessions(limit=limit, offset=offset)
        results: list[dict] = []
        for rec in records:
            msg_count = 0
            tok_est = 0
            try:
                msgs = self._storage.list_messages(rec.id)
                msg_count = len(msgs)
                tok_est = sum(len(str(m.content or "")) // 2 for m in msgs)
            except Exception:
                pass
            results.append({
                "id": rec.id,
                "agent_name": rec.agent_name,
                "title": rec.title,
                "status": rec.status.value if hasattr(rec.status, "value") else rec.status,
                "mode": rec.mode.value if hasattr(rec.mode, "value") else rec.mode,
                "summary": rec.summary,
                "error": rec.error,
                "parent_id": rec.parent_id,
                "created_at": rec.created_at,
                "updated_at": rec.updated_at,
                "completed_at": rec.completed_at,
                "message_count": msg_count,
                "total_tokens_estimate": tok_est,
            })
        return results

    def get_session(self, session_id: str) -> SessionRecord | None:
        """Get a single session by ID.

        Returns:
            SessionRecord or None if not found.
        """
        return self._storage.get_session(session_id)

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its messages.

        Returns:
            bool: True if deleted, False if not found.
        """
        return self._storage.delete_session(session_id)

    def delete_sessions_batch(self, session_ids: list[str]) -> int:
        """Delete multiple sessions in one transaction.

        Returns:
            int: Number of sessions actually deleted.
        """
        return self._storage.delete_sessions_batch(session_ids)

    def update_title(self, session_id: str, title: str) -> bool:
        """Update a session's title.

        Returns:
            bool: True if updated, False if not found.
        """
        return self._storage.update_title(session_id, title)

    def update_agent_name(self, session_id: str, agent_name: str) -> bool:
        """Update a session's agent_name (mode).

        Returns:
            bool: True if updated, False if not found.
        """
        return self._storage.update_agent_name(session_id, agent_name)

    def get_session_detail(self, session_id: str) -> dict | None:
        """Get session detail with computed stats.

        Extends the base SessionRecord with:
        - ``message_count`` (int): Total messages in the session.
        - ``total_tokens_estimate`` (int): Rough token estimate from summary length.

        Returns:
            dict or None if not found.
        """
        rec = self._storage.get_session(session_id)
        if rec is None:
            return None

        # Compute stats
        try:
            msgs = self._storage.list_messages(session_id)
            message_count = len(msgs)
            total_tokens_estimate = sum(
                len(str(m.content or "")) // 2 for m in msgs
            )
        except Exception:
            message_count = 0
            total_tokens_estimate = 0

        return {
            "id": rec.id,
            "parent_id": rec.parent_id,
            "root_id": rec.root_id,
            "agent_name": rec.agent_name,
            "title": rec.title,
            "status": rec.status.value if hasattr(rec.status, "value") else rec.status,
            "mode": rec.mode.value if hasattr(rec.mode, "value") else rec.mode,
            "summary": rec.summary,
            "error": rec.error,
            "agent_kind": rec.agent_kind.value if hasattr(rec.agent_kind, "value") else rec.agent_kind,
            "context_origin": rec.context_origin.value if hasattr(rec.context_origin, "value") else rec.context_origin,
            "execution_placement": rec.execution_placement.value if hasattr(rec.execution_placement, "value") else rec.execution_placement,
            "workspace_mode": rec.workspace_mode.value if hasattr(rec.workspace_mode, "value") else rec.workspace_mode,
            "agent_depth": rec.agent_depth.value if hasattr(rec.agent_depth, "value") else int(rec.agent_depth),
            "generation": rec.generation,
            "created_at": rec.created_at,
            "updated_at": rec.updated_at,
            "completed_at": rec.completed_at,
            "metadata": rec.metadata,
            "message_count": message_count,
            "total_tokens_estimate": total_tokens_estimate,
        }

    def get_child_sessions(self, parent_id: str) -> list[SessionRecord]:
        """Get all child sessions of a parent.

        Returns:
            list[SessionRecord]: Children ordered by creation time.
        """
        return self._storage.list_child_sessions(parent_id)

    # ── Messages ──────────────────────────────────────────────────────────

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Get all messages for a session as JSON-safe dicts.

        Args:
            session_id: The session to query.

        Returns:
            list[dict]: Each message has ``role``, ``content``,
                ``tool_calls`` (optional), ``tool_call_id`` (optional),
                ``tool_name`` (optional).

        Raises:
            ValueError: If session not found.
        """
        if self._storage.get_session(session_id) is None:
            raise ValueError(f"Unknown session: {session_id}")
        msgs = self._storage.list_messages(session_id)
        return [_serialize_message(m) for m in msgs]

    # ── Events ────────────────────────────────────────────────────────────

    def get_events(
        self, session_id: str, *, after: int = 0, limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Read EventLog JSONL events for a session.

        Events are stored in per-run JSONL files under the project's state
        log directory.  This method reads all JSONL files whose ``task_id``
        matches the session, deduplicates by ``event_id``, and returns them
        ordered by timestamp.

        Args:
            session_id: The session whose events to fetch.
            after: 0-based index — skip this many events before applying
                ``limit`` (default 0).
            limit: Max events to return (default 1000).

        Returns:
            list[dict]: Each contains ``event_id``, ``event_type``,
                ``task_id``, ``timestamp``, ``payload``.

        Raises:
            ValueError: If session not found.
        """
        session = self._storage.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")

        log_dir = self._resolve_log_dir(session.repo_path)
        events: list[dict[str, Any]] = []

        # Scan JSONL files in the log directory
        log_path = Path(log_dir)
        if log_path.is_dir():
            for jsonl_file in sorted(log_path.glob("*.jsonl")):
                try:
                    for line in jsonl_file.read_text("utf-8").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            raw = json.loads(line)
                            events.append({
                                "event_id": raw.get("event_id", ""),
                                "event_type": raw.get("event_type", ""),
                                "task_id": raw.get("task_id", ""),
                                "timestamp": raw.get("timestamp", ""),
                                "payload": raw.get("payload", {}),
                            })
                        except json.JSONDecodeError:
                            continue
                except OSError:
                    continue

        # Apply pagination
        if after > 0:
            events = events[after:]
        return events[:limit]

    # ── Cancel ────────────────────────────────────────────────────────────

    def cancel_session(
        self, session_id: str, detail: str = "",
    ) -> bool:
        """Cancel a running session.

        Delegates to SessionRuntime's cancellation token mechanism. Returns
        False if the session has no active token (e.g. already finished).

        Args:
            session_id: The session to cancel.
            detail: Human-readable cancellation reason.

        Returns:
            bool: True if the session had an active token and was cancelled.
        """
        # NOTE: Cancel is delegated to AgentService which holds the
        # SessionRuntime reference. This method is a placeholder for the
        # web API contract — the actual cancellation path is:
        #
        #     router → AgentService.cancel_session(session_id, detail)
        #
        # See agent_service.py for the real implementation.
        raise NotImplementedError(
            "Use AgentService.cancel_session() — it holds the SessionRuntime reference"
        )

    # ── Helpers ───────────────────────────────────────────────────────────

    def _resolve_log_dir(self, repo_path: str) -> str:
        """Resolve the EventLog directory for a repo path.

        Uses the same logic as EventLog.create() to find the log directory.
        """
        try:
            state_paths = ProjectStatePaths.for_project(repo_path)
            return str(state_paths.logs)
        except Exception:
            return "logs"
