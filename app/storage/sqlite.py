"""SQLite storage backend — wraps existing SessionStore behind StorageBackend.

This is a thin adapter that converts ``SessionStore`` method calls to the
``StorageBackend`` protocol.  No new SQL or table logic lives here — it
delegates entirely to ``agent/session/session_store.py``.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from agent.session.models import (
    AgentCompletionNotification,
    AgentKind,
    AgentRunResult,
    SessionMode,
    SessionRecord,
    SessionStatus,
)
from agent.session.session_store import SessionStore
from llm.base import LLMMessage

from .protocol import StorageBackend, StorageStats

logger = logging.getLogger(__name__)


class SqliteStorageBackend(StorageBackend):
    """SQLite implementation of StorageBackend.

    Wraps ``SessionStore`` from ``agent/session/session_store.py``.
    The database location is determined by ``default_session_db_path(repo_path)``.

    Usage::

        backend = SqliteStorageBackend(db_path)
        session = backend.create_session(
            agent_name="build", mode=SessionMode.PRIMARY,
            repo_path="/repo", title="My Session",
        )
    """

    def __init__(self, db_path: str) -> None:
        self._store = SessionStore(db_path)
        self._start_time = time.time()
        self._db_path = db_path
        logger.debug("SqliteStorageBackend initialized: %s", db_path)

    @property
    def store(self) -> SessionStore:
        """Access the underlying SessionStore (for advanced operations)."""
        return self._store

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
        return self._store.create_session(
            agent_name=agent_name,
            mode=mode,
            agent_kind=agent_kind,
            repo_path=repo_path,
            title=title,
            parent_id=parent_id,
            root_id=root_id,
            metadata=metadata,
        )

    def get_session(self, session_id: str) -> SessionRecord | None:
        return self._store.get_session(session_id)

    def list_sessions(
        self, limit: int = 50, offset: int = 0,
    ) -> list[SessionRecord]:
        return self._store.list_sessions(limit=limit, offset=offset)

    def update_status(
        self, session_id: str, status: SessionStatus, error: str = "",
    ) -> None:
        self._store.update_status(session_id, status, error=error)

    def set_summary(
        self, session_id: str, summary: str, *, status: SessionStatus,
    ) -> None:
        self._store.set_summary(session_id, summary, status=status)

    def delete_session(self, session_id: str) -> bool:
        session = self._store.get_session(session_id)
        if session is None:
            return False
        # SessionStore does not have a delete method yet.
        # Future: DELETE FROM sessions WHERE id = ?
        #        DELETE FROM session_messages WHERE session_id = ?
        raise NotImplementedError("Session deletion is not yet implemented in SessionStore")

    # ── Messages ──────────────────────────────────────────────────────────

    def append_message(
        self, session_id: str, message: LLMMessage,
    ) -> None:
        self._store.append_message(session_id, message)

    def list_messages(self, session_id: str) -> list[LLMMessage]:
        return self._store.list_messages(session_id)

    def count_messages(self, session_id: str) -> int:
        session = self._store.get_session(session_id)
        if session is None:
            return 0
        return len(self._store.list_messages(session_id))

    # ── Child / fork sessions ────────────────────────────────────────────

    def list_child_sessions(self, parent_id: str) -> list[SessionRecord]:
        return self._store.list_child_sessions(parent_id)

    # ── Agent notifications ──────────────────────────────────────────────

    def append_notification(
        self, notification: AgentCompletionNotification,
    ) -> None:
        self._store.append_agent_notification(notification)

    def claim_pending_notifications(
        self, parent_session_id: str,
    ) -> tuple[AgentCompletionNotification, ...]:
        return self._store.claim_pending_agent_notifications(parent_session_id)

    # ── Session resume ────────────────────────────────────────────────────

    def prepare_resume(
        self, session_id: str, message: LLMMessage,
    ) -> SessionRecord:
        return self._store.prepare_session_resume(session_id, message)

    # ── Agent result ──────────────────────────────────────────────────────

    def set_agent_result(
        self, session_id: str, result: AgentRunResult,
    ) -> None:
        self._store.set_agent_result(session_id, result)

    # ── Storage admin ─────────────────────────────────────────────────────

    def get_stats(self) -> StorageStats:
        """Return SQLite backend statistics."""
        db_size = None
        try:
            db_path = Path(self._db_path)
            if db_path.is_file():
                db_size = db_path.stat().st_size
        except OSError:
            pass

        total_sessions = 0
        total_messages = 0
        try:
            total_sessions = len(self._store.list_sessions(limit=0))
        except Exception:
            pass
        try:
            total_messages = sum(
                self._store.list_messages(s.id).__len__()
                for s in self._store.list_sessions(limit=100)
            )
        except Exception:
            pass

        return StorageStats(
            backend="sqlite",
            total_sessions=total_sessions,
            total_messages=total_messages,
            db_size_bytes=db_size,
            uptime_seconds=time.time() - self._start_time,
        )

    def ping(self) -> bool:
        try:
            with self._store._connect():
                return True
        except Exception:
            return False

    def close(self) -> None:
        """SQLite backend does not hold persistent connections — nothing to close."""
        pass
