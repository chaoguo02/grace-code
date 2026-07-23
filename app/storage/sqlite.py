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

_SESSION_TITLE_MAX_LENGTH = 200  # session title truncation limit (P2-20)


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
        self._init_stats_tables()
        self._init_memory_tables()
        logger.debug("SqliteStorageBackend initialized: %s", db_path)

    def _init_stats_tables(self) -> None:
        """Create stats/diff/review tables if they don't exist."""
        try:
            with self._store._connect() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS session_stats (
                        session_id TEXT PRIMARY KEY,
                        agent_name TEXT NOT NULL,
                        total_steps INTEGER NOT NULL DEFAULT 0,
                        total_tokens INTEGER NOT NULL DEFAULT 0,
                        total_duration_ms INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL,
                        tool_summary TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS step_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        step_number INTEGER NOT NULL,
                        tool_name TEXT NOT NULL,
                        tool_params TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'success',
                        duration_ms INTEGER NOT NULL DEFAULT 0,
                        tokens INTEGER NOT NULL DEFAULT 0,
                        timestamp TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_step_log_session
                        ON step_log(session_id, step_number);

                    CREATE TABLE IF NOT EXISTS session_diffs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        step_number INTEGER NOT NULL DEFAULT 0,
                        file_path TEXT NOT NULL,
                        diff_content TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        review_comment TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_session_diffs_session
                        ON session_diffs(session_id);

                    CREATE TABLE IF NOT EXISTS daily_rollup (
                        date TEXT PRIMARY KEY,
                        session_count INTEGER NOT NULL DEFAULT 0,
                        total_tokens INTEGER NOT NULL DEFAULT 0,
                        total_duration_ms INTEGER NOT NULL DEFAULT 0,
                        tool_summary TEXT NOT NULL DEFAULT '{}',
                        status_summary TEXT NOT NULL DEFAULT '{}'
                    );
                """)
        except Exception:
            logger.exception("Failed to create stats tables")

    def _init_memory_tables(self) -> None:
        """Create memory store tables if they don't exist."""
        try:
            with self._store._connect() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS memory_entries (
                        name TEXT PRIMARY KEY,
                        description TEXT NOT NULL,
                        content TEXT NOT NULL DEFAULT '',
                        type TEXT NOT NULL DEFAULT 'project',
                        status TEXT NOT NULL DEFAULT 'active',
                        scope TEXT NOT NULL DEFAULT 'project',
                        confidence REAL NOT NULL DEFAULT 0.7,
                        access_count INTEGER NOT NULL DEFAULT 0,
                        source TEXT NOT NULL DEFAULT '',
                        source_session_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        expires_at TEXT
                    );

                    CREATE TABLE IF NOT EXISTS memory_anchors (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        memory_name TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        path TEXT,
                        symbol_name TEXT,
                        task_value TEXT,
                        content_hash TEXT,
                        FOREIGN KEY (memory_name) REFERENCES memory_entries(name) ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_memory_type ON memory_entries(type);
                    CREATE INDEX IF NOT EXISTS idx_memory_status ON memory_entries(status);
                    CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_entries(scope);
                    CREATE INDEX IF NOT EXISTS idx_memory_confidence ON memory_entries(confidence DESC);
                    CREATE INDEX IF NOT EXISTS idx_memory_anchors_name ON memory_anchors(memory_name);
                """)
                # Migration: add expires_at column to existing databases
                try:
                    conn.execute(
                        "ALTER TABLE memory_entries ADD COLUMN expires_at TEXT"
                    )
                except sqlite3.OperationalError:
                    pass  # column already exists
        except Exception:
            logger.exception("Failed to create memory tables")

        # ── Plan revisions table ──────────────────────────────────────
        try:
            with self._store._connect() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS plan_revisions (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        content TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        parent_revision INTEGER DEFAULT 0,
                        change_request TEXT DEFAULT '',
                        status TEXT DEFAULT 'pending',
                        created_at TEXT NOT NULL,
                        FOREIGN KEY (session_id) REFERENCES sessions(id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_plan_rev_session
                        ON plan_revisions(session_id, revision);
                """)
        except Exception:
            logger.exception("Failed to create plan_revisions table")

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
        try:
            with self._store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM session_messages WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM agent_notifications WHERE parent_session_id = ?", (session_id,))
                conn.execute("DELETE FROM agent_notifications WHERE child_session_id = ?", (session_id,))
                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                conn.execute("COMMIT")
            return True
        except Exception:
            logger.exception("Failed to delete session %s", session_id)
            # COMMIT failure or any SQL error → rollback automatically
            # when the connection context exits
            return False

    def delete_sessions_batch(self, session_ids: list[str]) -> int:
        """Delete multiple sessions in one transaction. Returns count deleted."""
        if not session_ids:
            return 0
        deleted = 0
        try:
            with self._store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                for sid in session_ids:
                    conn.execute("DELETE FROM session_messages WHERE session_id = ?", (sid,))
                    conn.execute("DELETE FROM agent_notifications WHERE parent_session_id = ?", (sid,))
                    conn.execute("DELETE FROM agent_notifications WHERE child_session_id = ?", (sid,))
                    c = conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
                    if c.rowcount > 0:
                        deleted += 1
                conn.execute("COMMIT")
            logger.info("Batch deleted %d/%d sessions", deleted, len(session_ids))
            return deleted
        except Exception:
            # COMMIT failure or any SQL error → rollback automatically
            # when the connection context exits
            logger.exception("Failed to batch delete sessions")
            return deleted

    def update_title(self, session_id: str, title: str) -> bool:
        """Update a session's title. Returns True if updated."""
        session = self._store.get_session(session_id)
        if session is None:
            return False
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            with self._store._connect() as conn:
                conn.execute(
                    "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                    (title[:_SESSION_TITLE_MAX_LENGTH], now, session_id),
                )
            return True
        except Exception:
            logger.exception("Failed to update title for %s", session_id)
            return False

    def update_agent_name(self, session_id: str, agent_name: str) -> bool:
        """Update a session's agent_name. Returns True if updated."""
        session = self._store.get_session(session_id)
        if session is None:
            return False
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            with self._store._connect() as conn:
                conn.execute(
                    "UPDATE sessions SET agent_name = ?, updated_at = ? WHERE id = ?",
                    (agent_name, now, session_id),
                )
            return True
        except Exception:
            logger.exception("Failed to update agent_name for %s", session_id)
            return False

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

    # ── Execution stats ──────────────────────────────────────────────────

    def upsert_session_stats(
        self, session_id: str, *, agent_name: str, total_steps: int,
        total_tokens: int, total_duration_ms: int, status: str,
        tool_summary: str,
    ) -> None:
        try:
            with self._store._connect() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO session_stats
                       (session_id, agent_name, total_steps, total_tokens,
                        total_duration_ms, status, tool_summary, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    (session_id, agent_name, total_steps, total_tokens,
                     total_duration_ms, status, tool_summary),
                )
        except Exception:
            logger.exception("Failed to upsert session_stats %s", session_id)

    def insert_step_log(
        self, session_id: str, *, step_number: int, tool_name: str,
        tool_params: str, status: str, duration_ms: int, tokens: int,
        timestamp: str,
    ) -> None:
        try:
            with self._store._connect() as conn:
                conn.execute(
                    """INSERT INTO step_log
                       (session_id, step_number, tool_name, tool_params,
                        status, duration_ms, tokens, timestamp)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (session_id, step_number, tool_name, tool_params,
                     status, duration_ms, tokens, timestamp),
                )
        except Exception:
            logger.exception("Failed to insert step_log %s step=%d",
                             session_id, step_number)

    def insert_session_diff(
        self, session_id: str, *, step_number: int, file_path: str,
        diff_content: str,
    ) -> int:
        try:
            with self._store._connect() as conn:
                cur = conn.execute(
                    """INSERT INTO session_diffs
                       (session_id, step_number, file_path, diff_content,
                        status, created_at)
                       VALUES (?, ?, ?, ?, 'pending', datetime('now'))""",
                    (session_id, step_number, file_path, diff_content),
                )
                return cur.lastrowid or 0
        except Exception:
            logger.exception("Failed to insert session_diff %s", session_id)
            return 0

    def get_session_diffs(
        self, session_id: str, status: str | None = None,
    ) -> list[dict]:
        try:
            with self._store._connect() as conn:
                if status:
                    rows = conn.execute(
                        "SELECT * FROM session_diffs WHERE session_id=? AND status=? ORDER BY id",
                        (session_id, status),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM session_diffs WHERE session_id=? ORDER BY id",
                        (session_id,),
                    ).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            logger.exception("Failed to get_session_diffs %s", session_id)
            return []

    def update_diff_status(
        self, diff_id: int, status: str, comment: str = "",
    ) -> bool:
        try:
            with self._store._connect() as conn:
                cur = conn.execute(
                    "UPDATE session_diffs SET status=?, review_comment=? WHERE id=?",
                    (status, comment, diff_id),
                )
                return cur.rowcount > 0
        except Exception:
            logger.exception("Failed to update_diff_status %d", diff_id)
            return False

    def upsert_daily_rollup(
        self, date: str, *, session_count: int, total_tokens: int,
        total_duration_ms: int, tool_summary: str, status_summary: str,
    ) -> None:
        try:
            with self._store._connect() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO daily_rollup
                       (date, session_count, total_tokens, total_duration_ms,
                        tool_summary, status_summary)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (date, session_count, total_tokens, total_duration_ms,
                     tool_summary, status_summary),
                )
        except Exception:
            logger.exception("Failed to upsert daily_rollup %s", date)

    def get_daily_rollups(self, days: int = 30) -> list[dict]:
        try:
            with self._store._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM daily_rollup ORDER BY date DESC LIMIT ?",
                    (days,),
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            logger.exception("Failed to get_daily_rollups")
            return []

    def get_session_stats(self, session_id: str) -> dict | None:
        try:
            with self._store._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM session_stats WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                return dict(row) if row else None
        except Exception:
            return None

    def get_session_steps(self, session_id: str) -> list[dict]:
        try:
            with self._store._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM step_log WHERE session_id=? ORDER BY step_number",
                    (session_id,),
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []

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
            with self._store._connect() as conn:
                row = conn.execute("SELECT COUNT(*) AS cnt FROM sessions").fetchone()
                if row:
                    total_sessions = row["cnt"]
        except Exception:
            pass
        try:
            with self._store._connect() as conn:
                row = conn.execute("SELECT COUNT(*) AS cnt FROM session_messages").fetchone()
                if row:
                    total_messages = row["cnt"]
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

    # ── Plan revisions ──────────────────────────────────────────────────

    def insert_plan_revision(self, rev: dict) -> None:
        with self._store._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO plan_revisions
                   (id, session_id, revision, content, content_hash,
                    parent_revision, change_request, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (rev["id"], rev["session_id"], rev["revision"], rev["content"],
                 rev["content_hash"], rev.get("parent_revision", 0),
                 rev.get("change_request", ""), rev.get("status", "pending"),
                 rev["created_at"]),
            )

    def list_plan_revisions(self, session_id: str) -> list[dict]:
        with self._store._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM plan_revisions WHERE session_id = ? ORDER BY revision",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_plan_revision(self, session_id: str, revision: int) -> dict | None:
        with self._store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM plan_revisions WHERE session_id = ? AND revision = ?",
                (session_id, revision),
            ).fetchone()
        return dict(row) if row else None

    def update_plan_revision_status(self, session_id: str, revision: int, status: str) -> bool:
        with self._store._connect() as conn:
            cur = conn.execute(
                "UPDATE plan_revisions SET status = ? WHERE session_id = ? AND revision = ?",
                (status, session_id, revision),
            )
        return cur.rowcount > 0

    def close(self) -> None:
        """SQLite backend does not hold persistent connections — nothing to close."""
        pass

