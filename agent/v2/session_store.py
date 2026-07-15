from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent.task import ToolCall
from agent.v2.models import (
    ForkResult,
    SessionMode,
    SessionRecord,
    SessionStatus,
    WorktreeDisposition,
)
from llm.base import LLMMessage


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = str(Path(db_path))
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @property
    def db_path(self) -> str:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    parent_id TEXT NULL,
                    root_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    repo_path TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    fork_result_json TEXT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT NULL
                );

                CREATE TABLE IF NOT EXISTS session_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tool_call_id TEXT NULL,
                    tool_name TEXT NULL,
                    tool_calls_json TEXT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_parent_id
                    ON sessions(parent_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_root_id
                    ON sessions(root_id);
                CREATE INDEX IF NOT EXISTS idx_session_messages_session_id_id
                    ON session_messages(session_id, id);
                """
            )
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(sessions)")
            }
            if "fork_result_json" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN fork_result_json TEXT NULL")

    def create_session(
        self,
        *,
        agent_name: str,
        mode: SessionMode,
        repo_path: str,
        title: str,
        parent_id: str | None = None,
        root_id: str | None = None,
        metadata: dict | None = None,
    ) -> SessionRecord:
        mode = SessionMode(mode)
        parent = None
        if parent_id is not None:
            parent = self.get_session(parent_id)
            if parent is None:
                raise ValueError(f"Unknown parent session: {parent_id}")
            if mode is not SessionMode.SUBAGENT:
                raise ValueError("A session with parent_id must use subagent mode")
            if root_id is not None and root_id != parent.root_id:
                raise ValueError(
                    "Child root_id must match its parent root_id: "
                    f"parent={parent.root_id!r}, child={root_id!r}"
                )
        elif mode is SessionMode.SUBAGENT:
            raise ValueError("A subagent session requires parent_id")

        session_id = uuid.uuid4().hex[:12]
        resolved_root_id = parent.root_id if parent is not None else (root_id or session_id)
        now = _utc_now()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    id, parent_id, root_id, agent_name, mode, title, status,
                    repo_path, summary, error, metadata_json, created_at,
                    updated_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '', ?, ?, ?, NULL)
                """,
                (
                    session_id,
                    parent_id,
                    resolved_root_id,
                    agent_name,
                    mode.value,
                    title,
                    SessionStatus.QUEUED.value,
                    repo_path,
                    metadata_json,
                    now,
                    now,
                ),
            )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_session(row)

    def list_child_sessions(self, parent_id: str) -> list[SessionRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE parent_id = ? ORDER BY created_at, id",
                (parent_id,),
            ).fetchall()
        return [self._row_to_session(row) for row in rows]

    def list_worktree_sessions(
        self,
        dispositions: frozenset[WorktreeDisposition],
    ) -> list[SessionRecord]:
        """Return typed worktree sessions selected by lifecycle disposition."""
        if not dispositions:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sessions
                WHERE fork_result_json IS NOT NULL
                ORDER BY created_at, id
                """
            ).fetchall()
        records = [self._row_to_session(row) for row in rows]
        return [
            record for record in records
            if (
                record.fork_result is not None
                and record.fork_result.worktree_disposition in dispositions
            )
        ]

    def append_message(self, session_id: str, message: LLMMessage) -> None:
        if self.get_session(session_id) is None:
            raise ValueError(f"Unknown v2 session: {session_id}")
        tool_name = None
        tool_calls_json = None
        if message.tool_calls:
            tool_calls_json = json.dumps(
                [tool_call.to_dict() for tool_call in message.tool_calls],
                ensure_ascii=True,
            )
            tool_name = ",".join(tc.name for tc in message.tool_calls)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO session_messages (
                    session_id, role, content, tool_call_id, tool_name,
                    tool_calls_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    message.role,
                    str(message.content),
                    message.tool_call_id,
                    tool_name,
                    tool_calls_json,
                    _utc_now(),
                ),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (_utc_now(), session_id),
            )

    def list_messages(self, session_id: str) -> list[LLMMessage]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, role, content, tool_call_id, tool_name,
                       tool_calls_json, created_at
                FROM session_messages
                WHERE session_id = ?
                ORDER BY id
                """,
                (session_id,),
            ).fetchall()
        result: list[LLMMessage] = []
        for row in rows:
            tool_calls = None
            raw_tool_calls = row["tool_calls_json"]
            if raw_tool_calls:
                tool_calls = [
                    ToolCall(name=tc["name"], params=tc["params"], id=tc.get("id"))
                    for tc in json.loads(raw_tool_calls)
                ]
            result.append(LLMMessage(
                role=row["role"],
                content=row["content"],
                tool_call_id=row["tool_call_id"],
                tool_calls=tool_calls,
            ))
        return result

    def update_status(
        self, session_id: str, status: SessionStatus, error: str = ""
    ) -> None:
        status = SessionStatus(status)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE sessions
                SET status = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status.value, error, _utc_now(), session_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown v2 session: {session_id}")

    def set_summary(
        self, session_id: str, summary: str, *, status: SessionStatus
    ) -> None:
        status = SessionStatus(status)
        now = _utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE sessions
                SET summary = ?, status = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (summary, status.value, now, now, session_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown v2 session: {session_id}")

    def set_fork_result(self, session_id: str, result: ForkResult) -> None:
        """Persist the typed child result at the session boundary."""
        payload = json.dumps(result.to_dict(), ensure_ascii=True)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE sessions
                SET fork_result_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (payload, _utc_now(), session_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown v2 session: {session_id}")

    def touch_session(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (_utc_now(), session_id),
            )

    def _row_to_session(self, row: sqlite3.Row) -> SessionRecord:
        raw_fork_result = row["fork_result_json"]
        return SessionRecord(
            id=row["id"],
            parent_id=row["parent_id"],
            root_id=row["root_id"],
            agent_name=row["agent_name"],
            mode=SessionMode(row["mode"]),
            title=row["title"],
            status=SessionStatus(row["status"]),
            repo_path=row["repo_path"],
            summary=row["summary"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            metadata=json.loads(row["metadata_json"] or "{}"),
            fork_result=(
                ForkResult.from_dict(json.loads(raw_fork_result))
                if raw_fork_result else None
            ),
        )
