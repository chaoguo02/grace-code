"""SQLite storage backend — wraps existing SessionStore behind StorageBackend.

This is a thin adapter that converts ``SessionStore`` method calls to the
``StorageBackend`` protocol.  No new SQL or table logic lives here — it
delegates entirely to ``agent/session/session_store.py``.
"""

from __future__ import annotations

import logging
import os
import json
import sqlite3
import time
from datetime import datetime, timezone
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

                    CREATE TABLE IF NOT EXISTS context_snapshot (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        run_id TEXT NOT NULL DEFAULT '',
                        turn_id TEXT NOT NULL DEFAULT '',
                        step_number INTEGER NOT NULL,
                        request_kind TEXT NOT NULL DEFAULT 'primary',
                        stats_json TEXT NOT NULL DEFAULT '{}',
                        capabilities_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL DEFAULT (datetime('now'))
                    );

                    CREATE INDEX IF NOT EXISTS idx_context_snapshot_session
                        ON context_snapshot(session_id, id);

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

                    CREATE TABLE IF NOT EXISTS session_trace_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        seq INTEGER NOT NULL,
                        event_type TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        event_json TEXT NOT NULL,
                        source TEXT NOT NULL DEFAULT 'event_bus',
                        child_session_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT (datetime('now')),
                        UNIQUE(session_id, seq)
                    );

                    CREATE INDEX IF NOT EXISTS idx_trace_events_session_seq
                        ON session_trace_events(session_id, seq);
                    CREATE INDEX IF NOT EXISTS idx_trace_events_session_type
                        ON session_trace_events(session_id, event_type);

                    CREATE TABLE IF NOT EXISTS runs (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        turn_index INTEGER NOT NULL,
                        idempotency_key TEXT NOT NULL DEFAULT '',
                        prompt TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'queued',
                        summary TEXT NOT NULL DEFAULT '',
                        steps_taken INTEGER NOT NULL DEFAULT 0,
                        total_tokens INTEGER NOT NULL DEFAULT 0,
                        error TEXT,
                        termination_reason TEXT NOT NULL DEFAULT 'none',
                        verification_status TEXT NOT NULL DEFAULT 'not_applicable',
                        verification_reason TEXT NOT NULL DEFAULT 'none',
                        verification_checks_json TEXT NOT NULL DEFAULT '[]',
                        workspace_delta_json TEXT NOT NULL DEFAULT '{}',
                        started_at TEXT,
                        completed_at TEXT,
                        created_at TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                        FOREIGN KEY (session_id) REFERENCES sessions(id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_runs_session_created
                        ON runs(session_id, created_at);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_idempotency
                        ON runs(session_id, idempotency_key)
                        WHERE idempotency_key != '';
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_one_active
                        ON runs(session_id)
                        WHERE status IN ('queued', 'running');

                    CREATE TABLE IF NOT EXISTS daily_rollup (
                        date TEXT PRIMARY KEY,
                        session_count INTEGER NOT NULL DEFAULT 0,
                        total_tokens INTEGER NOT NULL DEFAULT 0,
                        total_duration_ms INTEGER NOT NULL DEFAULT 0,
                        tool_summary TEXT NOT NULL DEFAULT '{}',
                        status_summary TEXT NOT NULL DEFAULT '{}'
                    );

                    CREATE TABLE IF NOT EXISTS llm_turn_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        run_id TEXT NOT NULL DEFAULT '',
                        turn_id TEXT NOT NULL DEFAULT '',
                        step_number INTEGER NOT NULL,
                        input_tokens INTEGER NOT NULL DEFAULT 0,
                        output_tokens INTEGER NOT NULL DEFAULT 0,
                        billable_tokens INTEGER NOT NULL DEFAULT 0,
                        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                        cache_create_tokens INTEGER NOT NULL DEFAULT 0,
                        non_cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                        token_source TEXT NOT NULL DEFAULT 'estimate',
                        attempts INTEGER NOT NULL DEFAULT 1,
                        retries INTEGER NOT NULL DEFAULT 0,
                        backoff_ms REAL NOT NULL DEFAULT 0,
                        timed_out INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT (datetime('now'))
                    );
                    CREATE INDEX IF NOT EXISTS idx_llm_turn_metrics_session
                        ON llm_turn_metrics(session_id, id);
                """)
        except Exception:
            logger.exception("Failed to create stats tables")
        # ── Migrations ──
        try:
            with self._store._connect() as conn:
                conn.execute(
                    "ALTER TABLE session_messages ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''"
                )
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            with self._store._connect() as conn:
                run_columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(runs)")
                }
                additions = {
                    "termination_reason": "TEXT NOT NULL DEFAULT 'none'",
                    "verification_status": "TEXT NOT NULL DEFAULT 'not_applicable'",
                    "verification_reason": "TEXT NOT NULL DEFAULT 'none'",
                    "verification_checks_json": "TEXT NOT NULL DEFAULT '[]'",
                    "workspace_delta_json": "TEXT NOT NULL DEFAULT '{}'",
                }
                for name, declaration in additions.items():
                    if name not in run_columns:
                        conn.execute(
                            f"ALTER TABLE runs ADD COLUMN {name} {declaration}"
                        )
        except Exception:
            logger.exception("Failed to migrate structured run outcome columns")
        try:
            with self._store._connect() as conn:
                context_columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(context_snapshot)")
                }
                for name in ("run_id", "turn_id"):
                    if name not in context_columns:
                        conn.execute(
                            f"ALTER TABLE context_snapshot "
                            f"ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                        )
        except Exception:
            logger.exception("Failed to migrate context snapshot identity columns")

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

    def recover_orphaned_runs(self) -> list[dict[str, str]]:
        """Atomically fail active runs left by a previous server process."""
        now = datetime.now(timezone.utc).isoformat()
        detail = "Interrupted by server restart"
        with self._store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    """SELECT id, session_id FROM runs
                       WHERE status IN ('queued', 'running')"""
                ).fetchall()
                if rows:
                    conn.executemany(
                        """UPDATE runs
                           SET status='failed', error=?,
                               termination_reason='internal_error',
                               completed_at=?, updated_at=?
                           WHERE id=? AND status IN ('queued', 'running')""",
                        [
                            (detail, now, now, str(row["id"]))
                            for row in rows
                        ],
                    )
                dangling = conn.execute(
                    """SELECT s.id AS session_id,
                              (SELECT r.id FROM runs r
                               WHERE r.session_id=s.id
                               ORDER BY r.turn_index DESC, r.created_at DESC
                               LIMIT 1) AS run_id,
                              (SELECT r.status FROM runs r
                               WHERE r.session_id=s.id
                               ORDER BY r.turn_index DESC, r.created_at DESC
                               LIMIT 1) AS run_status,
                              (SELECT r.error FROM runs r
                               WHERE r.session_id=s.id
                               ORDER BY r.turn_index DESC, r.created_at DESC
                               LIMIT 1) AS run_error
                       FROM sessions s
                       WHERE s.status='running'
                         AND NOT EXISTS (
                           SELECT 1 FROM runs active
                           WHERE active.session_id=s.id
                             AND active.status IN ('queued', 'running')
                         )"""
                ).fetchall()
                for item in dangling:
                    run_status = str(item["run_status"] or "")
                    session_status = (
                        "cancelled" if run_status == "cancelled"
                        else "completed" if run_status == "completed"
                        else "failed"
                    )
                    error = (
                        str(item["run_error"] or "")
                        if session_status in {"cancelled", "failed"}
                        else ""
                    ) or detail
                    conn.execute(
                        """UPDATE sessions
                           SET status=?, error=?, updated_at=?,
                               completed_at=COALESCE(completed_at, ?)
                           WHERE id=? AND status='running'""",
                        (
                            session_status,
                            error if session_status != "completed" else "",
                            now,
                            now,
                            str(item["session_id"]),
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        recovered = [
            {"run_id": str(row["id"]), "session_id": str(row["session_id"])}
            for row in rows
        ]
        active_session_ids = {item["session_id"] for item in recovered}
        recovered.extend(
            {
                "run_id": str(item["run_id"] or ""),
                "session_id": str(item["session_id"]),
            }
            for item in dangling
            if str(item["session_id"]) not in active_session_ids
        )
        return recovered

    def set_summary(
        self, session_id: str, summary: str, *, status: SessionStatus,
    ) -> None:
        self._store.set_summary(session_id, summary, status=status)

    # ── Runs ────────────────────────────────────────────────────────────────

    def create_run(
        self,
        *,
        run_id: str,
        session_id: str,
        turn_id: str,
        turn_index: int,
        prompt: str,
        idempotency_key: str = "",
    ) -> dict:
        """Create a new run record. Returns the run as a dict."""
        try:
            from datetime import datetime, timezone
            with self._store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """INSERT INTO runs
                       (id, session_id, turn_id, turn_index, idempotency_key, prompt,
                        status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                    (run_id, session_id, turn_id, turn_index, idempotency_key, prompt,
                     now, now),
                )
                conn.execute("COMMIT")
            return {
                "id": run_id, "session_id": session_id, "turn_id": turn_id,
                "turn_index": turn_index, "status": "queued", "prompt": prompt,
            }
        except Exception:
            logger.exception("Failed to create run %s", run_id)
            raise

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        summary: str | None = None,
        steps_taken: int | None = None,
        total_tokens: int | None = None,
        error: str | None = None,
        termination_reason: str | None = None,
        verification_status: str | None = None,
        verification_reason: str | None = None,
        verification_checks: list[dict] | None = None,
        workspace_delta: dict | None = None,
        expect_status: str | None = None,
    ) -> bool:
        """CAS update a run record.

        When *expect_status* is set, the UPDATE is conditional on the
        current status matching — preventing lost updates from concurrent
        cancel vs complete races.

        Returns True if a row was updated.
        """
        try:
            with self._store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                parts = ["updated_at = datetime('now')"]
                params: list = []

                if status is not None:
                    parts.append("status = ?")
                    params.append(status)
                    if status in {"completed", "failed", "cancelled"}:
                        parts.append("completed_at = datetime('now')")

                if summary is not None:
                    parts.append("summary = ?")
                    params.append(summary)
                if steps_taken is not None:
                    parts.append("steps_taken = ?")
                    params.append(steps_taken)
                if total_tokens is not None:
                    parts.append("total_tokens = ?")
                    params.append(total_tokens)
                if error is not None:
                    parts.append("error = ?")
                    params.append(error)
                if termination_reason is not None:
                    parts.append("termination_reason = ?")
                    params.append(termination_reason)
                if verification_status is not None:
                    parts.append("verification_status = ?")
                    params.append(verification_status)
                if verification_reason is not None:
                    parts.append("verification_reason = ?")
                    params.append(verification_reason)
                if verification_checks is not None:
                    parts.append("verification_checks_json = ?")
                    params.append(json.dumps(verification_checks, ensure_ascii=False))
                if workspace_delta is not None:
                    parts.append("workspace_delta_json = ?")
                    params.append(json.dumps(workspace_delta, ensure_ascii=False))

                where = "id = ?"
                params.append(run_id)

                if expect_status is not None:
                    where += " AND status = ?"
                    params.append(expect_status)

                cur = conn.execute(
                    f"UPDATE runs SET {', '.join(parts)} WHERE {where}",
                    params,
                )
                conn.execute("COMMIT")
                return cur.rowcount == 1
        except Exception:
            logger.exception("Failed to update run %s", run_id)
            return False

    def get_run(self, run_id: str) -> dict | None:
        """Get a single run by ID."""
        try:
            with self._store._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                return dict(row) if row else None
        except Exception:
            logger.exception("Failed to get run %s", run_id)
            return None

    def get_active_run(self, session_id: str) -> dict | None:
        """Get the currently active (queued or running) run for a session."""
        try:
            with self._store._connect() as conn:
                row = conn.execute(
                    """SELECT * FROM runs
                       WHERE session_id = ? AND status IN ('queued', 'running')
                       ORDER BY created_at DESC LIMIT 1""",
                    (session_id,),
                ).fetchone()
                return dict(row) if row else None
        except Exception:
            logger.exception("Failed to get active run for %s", session_id)
            return None

    def list_runs(
        self, session_id: str, *, limit: int = 20,
    ) -> list[dict]:
        """List runs for a session, newest first."""
        try:
            with self._store._connect() as conn:
                rows = conn.execute(
                    """SELECT * FROM runs WHERE session_id = ?
                       ORDER BY created_at DESC LIMIT ?""",
                    (session_id, limit),
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            logger.exception("Failed to list runs for %s", session_id)
            return []

    def check_idempotent_run(
        self, session_id: str, idempotency_key: str,
    ) -> dict | None:
        """Check if a run with this idempotency key already exists.

        Returns the run dict if found, None otherwise.
        """
        try:
            with self._store._connect() as conn:
                row = conn.execute(
                    """SELECT * FROM runs
                       WHERE session_id = ? AND idempotency_key = ?""",
                    (session_id, idempotency_key),
                ).fetchone()
                return dict(row) if row else None
        except Exception:
            logger.exception("Failed to check idempotent run %s/%s",
                             session_id, idempotency_key)
            return None

    def transactional_finalize_run(
        self,
        run_id: str,
        terminal_event: dict,
        session_id: str,
        *,
        summary: str = "",
        steps_taken: int = 0,
        total_tokens: int = 0,
        error: str = "",
        expect_status: str = "running",
    ) -> dict | None:
        """CAS-update Run + insert run_terminal trace event in ONE transaction.

        This guarantees that run_terminal is always in the EventStore when
        the Run transitions to a terminal state — no gap between commit
        and broadcast.

        Returns the terminal_event dict with ``sequence`` injected,
        or None if CAS failed (run already in a different state).
        """
        import json as _json
        try:
            with self._store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")

                # 1. CAS update Run
                cur = conn.execute(
                    """UPDATE runs SET status = ?, summary = ?, steps_taken = ?,
                       total_tokens = ?, error = ?, completed_at = datetime('now'),
                       updated_at = datetime('now')
                       WHERE id = ? AND status = ?""",
                    (terminal_event.get("status", "completed"),
                     summary, steps_taken, total_tokens, error,
                     run_id, expect_status),
                )
                if cur.rowcount != 1:
                    conn.execute("ROLLBACK")
                    return None  # CAS failed

                # 2. Insert run_terminal into trace_events (get atomic sequence)
                row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM session_trace_events WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                sequence = row[0] if row else 1

                event_type = str(terminal_event.get("type") or "run_terminal")
                timestamp = str(terminal_event.get("timestamp") or "")
                child_session_id = str(terminal_event.get("child_session_id") or "")

                stored = {**terminal_event, "seq": sequence, "sequence": sequence}
                conn.execute(
                    """INSERT INTO session_trace_events
                       (session_id, seq, event_type, timestamp, event_json, source, child_session_id)
                       VALUES (?, ?, ?, ?, ?, 'run_terminal', ?)""",
                    (session_id, sequence, event_type, timestamp,
                     _json.dumps(stored, ensure_ascii=False), child_session_id),
                )

                conn.execute("COMMIT")
                return stored
        except Exception:
            logger.exception("Failed to transactional_finalize_run %s", run_id)
            return None

    def delete_session(self, session_id: str) -> bool:
        session = self._store.get_session(session_id)
        if session is None:
            return False
        try:
            with self._store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM session_messages WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM session_trace_events WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM context_snapshot WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM agent_notifications WHERE parent_session_id = ?", (session_id,))
                conn.execute("DELETE FROM agent_notifications WHERE child_session_id = ?", (session_id,))
                conn.execute(
                    """DELETE FROM delegation_tasks
                       WHERE delegation_run_id IN (
                           SELECT id FROM delegation_runs
                           WHERE parent_session_id = ?
                       )""",
                    (session_id,),
                )
                conn.execute(
                    "DELETE FROM delegation_runs WHERE parent_session_id = ?",
                    (session_id,),
                )
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
                    conn.execute("DELETE FROM session_trace_events WHERE session_id = ?", (sid,))
                    conn.execute("DELETE FROM context_snapshot WHERE session_id = ?", (sid,))
                    conn.execute("DELETE FROM agent_notifications WHERE parent_session_id = ?", (sid,))
                    conn.execute("DELETE FROM agent_notifications WHERE child_session_id = ?", (sid,))
                    conn.execute(
                        """DELETE FROM delegation_tasks
                           WHERE delegation_run_id IN (
                               SELECT id FROM delegation_runs
                               WHERE parent_session_id = ?
                           )""",
                        (sid,),
                    )
                    conn.execute(
                        "DELETE FROM delegation_runs WHERE parent_session_id = ?",
                        (sid,),
                    )
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

    def update_metadata(self, session_id: str, metadata: dict) -> bool:
        """Replace session metadata through the public storage boundary."""
        session = self._store.get_session(session_id)
        if session is None:
            return False
        try:
            import json
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).isoformat()
            with self._store._connect() as conn:
                conn.execute(
                    "UPDATE sessions SET metadata_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(metadata, ensure_ascii=True), now, session_id),
                )
            return True
        except Exception:
            logger.exception("Failed to update metadata for %s", session_id)
            return False

    # ── Messages ──────────────────────────────────────────────────────────

    def append_message(
        self, session_id: str, message: LLMMessage,
    ) -> None:
        self._store.append_message(session_id, message)

    def list_messages(self, session_id: str) -> list[LLMMessage]:
        return self._store.list_messages(session_id)

    def optimize_storage(self) -> dict:
        """Run SQLite PRAGMA optimize to reclaim space and update statistics.

        Call after bulk deletes (compaction, session cleanup) to prevent
        index bloat and file fragmentation.
        Returns dict with before/after page counts.
        """
        try:
            with self._store._connect() as conn:
                before = conn.execute("PRAGMA page_count").fetchone()[0]
                conn.execute("PRAGMA optimize")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                after = conn.execute("PRAGMA page_count").fetchone()[0]
                return {"pages_before": before, "pages_after": after}
        except Exception:
            return {"pages_before": 0, "pages_after": 0}

    def replace_messages_with_compaction(
        self, session_id: str, messages: list[dict], **metadata,
    ) -> dict:
        return self._store.replace_messages_with_compaction(
            session_id, messages, **metadata
        )

    def list_compaction_runs(self, session_id: str) -> list[dict]:
        return self._store.list_compaction_runs(session_id)

    def list_archived_messages(self, session_id: str) -> list[dict]:
        return self._store.list_archived_messages(session_id)

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

    def insert_context_snapshot(
        self,
        session_id: str,
        *,
        run_id: str = "",
        turn_id: str = "",
        step_number: int,
        request_kind: str,
        stats_json: str,
        capabilities_json: str,
    ) -> int:
        """Persist one actually assembled provider-request context."""
        try:
            with self._store._connect() as conn:
                cur = conn.execute(
                    """INSERT INTO context_snapshot
                       (session_id, run_id, turn_id, step_number, request_kind,
                        stats_json, capabilities_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        run_id,
                        turn_id,
                        step_number,
                        request_kind,
                        stats_json,
                        capabilities_json,
                    ),
                )
                conn.execute(
                    """DELETE FROM context_snapshot
                       WHERE session_id=? AND id NOT IN (
                           SELECT id FROM context_snapshot
                           WHERE session_id=?
                           ORDER BY id DESC
                           LIMIT 500
                       )""",
                    (session_id, session_id),
                )
                return cur.lastrowid or 0
        except Exception:
            logger.exception(
                "Failed to insert context snapshot %s step=%d",
                session_id,
                step_number,
            )
            return 0

    def get_context_snapshots(
        self,
        session_id: str,
        *,
        limit: int = 200,
    ) -> list[dict]:
        """Return context snapshots in provider-request order."""
        try:
            with self._store._connect() as conn:
                rows = conn.execute(
                    """SELECT * FROM context_snapshot
                       WHERE session_id=?
                       ORDER BY id DESC
                       LIMIT ?""",
                    (session_id, max(1, min(limit, 1000))),
                ).fetchall()
                return [dict(row) for row in reversed(rows)]
        except Exception:
            logger.exception("Failed to get context snapshots %s", session_id)
            return []

    # ── Typed trace events ────────────────────────────────────────────────

    def insert_trace_event(
        self,
        session_id: str,
        event: dict,
        *,
        source: str = "event_bus",
    ) -> dict:
        try:
            import json
            from datetime import datetime, timezone

            event_type = str(event.get("type") or "event")
            timestamp = str(event.get("timestamp") or datetime.now(timezone.utc).isoformat())
            child_session_id = str(event.get("child_session_id") or "")
            # Auto-generate event_id for synthetic events that don't come from agent.task.Event
            if not event.get("event_id"):
                import uuid as _uuid
                event["event_id"] = str(_uuid.uuid4())
            with self._store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM session_trace_events WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                seq = int(row["next_seq"] if row else 1)
                # Use "sequence" in the event dict (DB column stays "seq" for backward compat)
                stored = { **event, "seq": seq, "sequence": seq }
                conn.execute(
                    """INSERT INTO session_trace_events
                       (session_id, seq, event_type, timestamp, event_json, source, child_session_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (session_id, seq, event_type, timestamp,
                     json.dumps(stored, ensure_ascii=False), source, child_session_id),
                )
                conn.execute("COMMIT")
                return stored
        except Exception:
            logger.exception("Failed to insert_trace_event %s type=%s", session_id, event.get("type"))
            return event

    def list_trace_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        limit: int = 200,
    ) -> list[dict]:
        try:
            import json
            with self._store._connect() as conn:
                rows = conn.execute(
                    """SELECT seq, event_json FROM session_trace_events
                       WHERE session_id=? AND seq>?
                       ORDER BY seq ASC LIMIT ?""",
                    (session_id, after_seq, limit),
                ).fetchall()
                events: list[dict] = []
                for row in rows:
                    try:
                        raw = json.loads(row["event_json"] or "{}")
                    except json.JSONDecodeError:
                        raw = {}
                    if isinstance(raw, dict):
                        raw.setdefault("seq", row["seq"])
                        raw.setdefault("sequence", row["seq"])
                        events.append(raw)
                return events
        except Exception:
            logger.exception("Failed to list_trace_events %s", session_id)
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
