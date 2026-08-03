from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent.task import ToolCall
from agent.session.models import (
    AgentCompletionNotification,
    AgentDepth,
    AgentKind,
    AgentRunResult,
    ContextOrigin,
    ExecutionPlacement,
    ForkResult,
    NotificationDeliveryState,
    SessionMode,
    SessionRecord,
    SessionStatus,
    WorktreeDisposition,
    WorkspaceMode,
)
from llm.base import LLMMessage
from agent.session.message_serializer import MessageKind


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = str(Path(db_path))
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        # P0_4: run schema migrations after base tables are created
        from agent.session.message_serializer import SchemaMigrator
        SchemaMigrator(self._db_path).ensure_latest()

    @property
    def db_path(self) -> str:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
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
                    agent_kind TEXT NOT NULL DEFAULT 'primary',
                    context_origin TEXT NOT NULL DEFAULT 'fresh',
                    execution_placement TEXT NOT NULL DEFAULT 'foreground',
                    workspace_mode TEXT NOT NULL DEFAULT 'current',
                    agent_depth INTEGER NOT NULL DEFAULT 0,
                    run_generation INTEGER NOT NULL DEFAULT 0,
                    agent_result_json TEXT NULL,
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
                    turn_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_message_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    compaction_id TEXT NOT NULL,
                    original_message_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tool_call_id TEXT NULL,
                    tool_name TEXT NULL,
                    tool_calls_json TEXT NULL,
                    turn_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    archived_at TEXT NOT NULL,
                    UNIQUE(compaction_id, original_message_id)
                );

                CREATE TABLE IF NOT EXISTS compaction_runs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    source_message_ids_json TEXT NOT NULL,
                    summary_hash TEXT NOT NULL,
                    method TEXT NOT NULL,
                    tokens_before INTEGER NOT NULL DEFAULT 0,
                    tokens_after INTEGER NOT NULL DEFAULT 0,
                    truncation_status TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT NULL,
                    UNIQUE(session_id, source_hash)
                );

                CREATE TABLE IF NOT EXISTS agent_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_session_id TEXT NOT NULL,
                    child_session_id TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    delivery_state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT NULL,
                    UNIQUE(child_session_id, generation)
                );

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

                CREATE TABLE IF NOT EXISTS delegation_runs (
                    id TEXT PRIMARY KEY,
                    parent_session_id TEXT NOT NULL,
                    parent_run_id TEXT NOT NULL DEFAULT '',
                    topology TEXT NOT NULL,
                    reason_code TEXT NOT NULL DEFAULT '',
                    explanation TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL DEFAULT 'executing',
                    budget_json TEXT NOT NULL DEFAULT '{}',
                    synthesis_json TEXT NULL,
                    verification_json TEXT NULL,
                    downgraded_from TEXT NULL,
                    is_team INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    interrupted_at TEXT NULL,
                    completed_at TEXT NULL
                );

                CREATE TABLE IF NOT EXISTS delegation_tasks (
                    id TEXT PRIMARY KEY,
                    delegation_run_id TEXT NOT NULL,
                    child_session_id TEXT NULL,
                    generation INTEGER NOT NULL DEFAULT 0,
                    agent_type TEXT NOT NULL,
                    purpose TEXT NOT NULL DEFAULT 'analysis',
                    goal TEXT NOT NULL,
                    prompt TEXT NOT NULL DEFAULT '',
                    scope_json TEXT NOT NULL DEFAULT '[]',
                    dependencies_json TEXT NOT NULL DEFAULT '[]',
                    expected_files_json TEXT NOT NULL DEFAULT '[]',
                    write_files_json TEXT NOT NULL DEFAULT '[]',
                    required INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    report_json TEXT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 1,
                    supersedes_task_id TEXT NULL,
                    integration_status TEXT NOT NULL DEFAULT 'not_required',
                    integration_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    started_at TEXT NULL,
                    completed_at TEXT NULL
                );

                CREATE TABLE IF NOT EXISTS run_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    root_run_id TEXT NOT NULL,
                    root_session_id TEXT DEFAULT '',
                    session_id TEXT NOT NULL,
                    producer_session_id TEXT NOT NULL,
                    turn_id TEXT DEFAULT '',
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    sequence INTEGER NOT NULL DEFAULT 0,
                    tool_name TEXT DEFAULT '',
                    call_id TEXT DEFAULT '',
                    invocation_id TEXT DEFAULT '',
                    parameters_digest TEXT DEFAULT '',
                    result_digest TEXT DEFAULT '',
                    source_fingerprint TEXT DEFAULT '',
                    cached INTEGER DEFAULT 0,
                    cache_key TEXT DEFAULT '',
                    path TEXT DEFAULT '',
                    artifact_id TEXT DEFAULT '',
                    depends_on_json TEXT DEFAULT '[]',
                    parent_evidence_id TEXT DEFAULT '',
                    summary TEXT DEFAULT '',
                    metadata_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE(root_run_id, idempotency_key)
                );

                CREATE INDEX IF NOT EXISTS idx_run_evidence_root
                    ON run_evidence(root_run_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_run_evidence_kind
                    ON run_evidence(root_run_id, kind, status);
                CREATE INDEX IF NOT EXISTS idx_run_evidence_call
                    ON run_evidence(root_run_id, call_id);
                CREATE INDEX IF NOT EXISTS idx_run_evidence_path
                    ON run_evidence(root_run_id, path);
                CREATE INDEX IF NOT EXISTS idx_run_evidence_producer
                    ON run_evidence(root_run_id, producer_session_id);

                CREATE INDEX IF NOT EXISTS idx_sessions_parent_id
                    ON sessions(parent_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_root_id
                    ON sessions(root_id);
                CREATE INDEX IF NOT EXISTS idx_session_messages_session_id_id
                    ON session_messages(session_id, id);
                CREATE INDEX IF NOT EXISTS idx_message_archive_session
                    ON session_message_archive(session_id, original_message_id);
                CREATE INDEX IF NOT EXISTS idx_compaction_runs_session
                    ON compaction_runs(session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_notifications_parent_state_id
                    ON agent_notifications(parent_session_id, delivery_state, id);
                CREATE INDEX IF NOT EXISTS idx_delegation_runs_parent
                    ON delegation_runs(parent_session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_delegation_tasks_run
                    ON delegation_tasks(delegation_run_id, created_at);
                """
            )
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(sessions)")
            }
            if "fork_result_json" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN fork_result_json TEXT NULL")
            message_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(session_messages)")
            }
            if "turn_id" not in message_columns:
                conn.execute(
                    "ALTER TABLE session_messages "
                    "ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''"
                )
            evidence_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(run_evidence)")
            }
            for name, declaration in {
                "root_session_id": "TEXT DEFAULT ''",
                "turn_id": "TEXT DEFAULT ''",
                "schema_version": "INTEGER NOT NULL DEFAULT 1",
                "sequence": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in evidence_columns:
                    conn.execute(
                        f"ALTER TABLE run_evidence ADD COLUMN {name} {declaration}"
                    )
            conn.execute(
                """
                UPDATE run_evidence
                SET sequence = (
                    SELECT COUNT(*)
                    FROM run_evidence AS prior
                    WHERE prior.root_run_id = run_evidence.root_run_id
                      AND prior.id <= run_evidence.id
                )
                WHERE sequence IS NULL OR sequence <= 0
                """
            )
            # Existing Phase-2 databases created this index before `sequence`
            # existed. Rebuild it once when its actual column set is stale.
            root_index_columns = [
                row["name"]
                for row in conn.execute(
                    "PRAGMA index_info(idx_run_evidence_root)"
                )
            ]
            if root_index_columns != ["root_run_id", "sequence"]:
                conn.execute("DROP INDEX IF EXISTS idx_run_evidence_root")
                conn.execute(
                    "CREATE INDEX idx_run_evidence_root "
                    "ON run_evidence(root_run_id, sequence)"
                )
            contract_columns = {
                "agent_kind": "TEXT NOT NULL DEFAULT 'primary'",
                "context_origin": "TEXT NOT NULL DEFAULT 'fresh'",
                "execution_placement": "TEXT NOT NULL DEFAULT 'foreground'",
                "workspace_mode": "TEXT NOT NULL DEFAULT 'current'",
                "agent_depth": "INTEGER NOT NULL DEFAULT 0",
                "run_generation": "INTEGER NOT NULL DEFAULT 0",
                "agent_result_json": "TEXT NULL",
            }
            legacy_contract_names = {
                "agent_kind", "context_origin",
                "execution_placement", "workspace_mode",
            }
            needs_legacy_contract_backfill = any(
                name not in columns for name in legacy_contract_names
            )
            for name, declaration in contract_columns.items():
                if name not in columns:
                    conn.execute(
                        f"ALTER TABLE sessions ADD COLUMN {name} {declaration}"
                    )
            conn.execute(
                """
                WITH RECURSIVE session_tree(id, depth) AS (
                    SELECT id, 0 FROM sessions WHERE parent_id IS NULL
                    UNION ALL
                    SELECT child.id, session_tree.depth + 1
                    FROM sessions AS child
                    JOIN session_tree ON child.parent_id = session_tree.id
                )
                UPDATE sessions
                SET agent_depth = (
                    SELECT depth FROM session_tree WHERE session_tree.id = sessions.id
                )
                WHERE id IN (SELECT id FROM session_tree)
                """
            )
            if needs_legacy_contract_backfill:
                rows = conn.execute(
                    "SELECT id, mode, metadata_json FROM sessions"
                ).fetchall()
                for row in rows:
                    metadata = json.loads(row["metadata_json"] or "{}")
                    legacy_workspace = metadata.get(
                        "workspace_mode", metadata.get("isolation")
                    )
                    workspace_mode = (
                        WorkspaceMode.WORKTREE
                        if legacy_workspace == "worktree"
                        else WorkspaceMode.CURRENT
                    )
                    agent_kind = (
                        AgentKind.PRIMARY
                        if row["mode"] == SessionMode.PRIMARY.value
                        else AgentKind.NAMED_SUBAGENT
                    )
                    conn.execute(
                        """
                        UPDATE sessions
                        SET agent_kind = ?, context_origin = ?,
                            execution_placement = ?, workspace_mode = ?
                        WHERE id = ?
                        """,
                        (
                            agent_kind.value,
                            ContextOrigin.FRESH.value,
                            ExecutionPlacement.FOREGROUND.value,
                            workspace_mode.value,
                            row["id"],
                        ),
                    )
            conn.execute(
                """
                UPDATE sessions
                SET agent_result_json = fork_result_json
                WHERE agent_result_json IS NULL
                  AND fork_result_json IS NOT NULL
                """
            )
            notification_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(agent_notifications)")
            }
            if "generation" not in notification_columns:
                conn.executescript(
                    """
                    DROP INDEX IF EXISTS idx_agent_notifications_parent_state_id;
                    ALTER TABLE agent_notifications
                        RENAME TO agent_notifications_legacy;
                    CREATE TABLE agent_notifications (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        parent_session_id TEXT NOT NULL,
                        child_session_id TEXT NOT NULL,
                        generation INTEGER NOT NULL DEFAULT 0,
                        payload_json TEXT NOT NULL,
                        delivery_state TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        delivered_at TEXT NULL,
                        UNIQUE(child_session_id, generation)
                    );
                    INSERT INTO agent_notifications (
                        id, parent_session_id, child_session_id, generation,
                        payload_json, delivery_state, created_at, delivered_at
                    )
                    SELECT id, parent_session_id, child_session_id, 0,
                           payload_json, delivery_state, created_at, delivered_at
                    FROM agent_notifications_legacy;
                    DROP TABLE agent_notifications_legacy;
                    CREATE INDEX idx_agent_notifications_parent_state_id
                        ON agent_notifications(
                            parent_session_id, delivery_state, id
                        );
                    """
                )
            delegation_run_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(delegation_runs)")
            }
            for name, declaration in {
                "phase": "TEXT NOT NULL DEFAULT 'executing'",
                "synthesis_json": "TEXT NULL",
                "verification_json": "TEXT NULL",
                "version": "INTEGER NOT NULL DEFAULT 0",
                "interrupted_at": "TEXT NULL",
            }.items():
                if name not in delegation_run_columns:
                    conn.execute(
                        f"ALTER TABLE delegation_runs ADD COLUMN {name} {declaration}"
                    )
            delegation_task_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(delegation_tasks)")
            }
            for name, declaration in {
                "prompt": "TEXT NOT NULL DEFAULT ''",
                "retry_count": "INTEGER NOT NULL DEFAULT 0",
                "max_retries": "INTEGER NOT NULL DEFAULT 1",
                "supersedes_task_id": "TEXT NULL",
                "integration_status": "TEXT NOT NULL DEFAULT 'not_required'",
                "integration_error": "TEXT NOT NULL DEFAULT ''",
                "resource_json": "TEXT NULL",
            }.items():
                if name not in delegation_task_columns:
                    conn.execute(
                        f"ALTER TABLE delegation_tasks ADD COLUMN {name} {declaration}"
                    )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_delegation_tasks_supersedes
                ON delegation_tasks(supersedes_task_id)
                WHERE supersedes_task_id IS NOT NULL
                """
            )

    def create_session(
        self,
        *,
        agent_name: str,
        mode: SessionMode,
        agent_kind: AgentKind | None = None,
        context_origin: ContextOrigin = ContextOrigin.FRESH,
        execution_placement: ExecutionPlacement = ExecutionPlacement.FOREGROUND,
        workspace_mode: WorkspaceMode = WorkspaceMode.CURRENT,
        repo_path: str,
        title: str,
        parent_id: str | None = None,
        root_id: str | None = None,
        metadata: dict | None = None,
    ) -> SessionRecord:
        mode = SessionMode(mode)
        agent_kind = AgentKind(
            agent_kind
            or (
                AgentKind.PRIMARY
                if mode is SessionMode.PRIMARY
                else AgentKind.NAMED_SUBAGENT
            )
        )
        context_origin = ContextOrigin(context_origin)
        execution_placement = ExecutionPlacement(execution_placement)
        workspace_mode = WorkspaceMode(workspace_mode)
        if (mode is SessionMode.PRIMARY) != (agent_kind is AgentKind.PRIMARY):
            raise ValueError("Session mode and agent kind must describe the same role")
        if execution_placement is ExecutionPlacement.AUTO:
            raise ValueError("Session creation requires a resolved execution placement")
        if (
            context_origin is ContextOrigin.PARENT_SNAPSHOT
            and agent_kind is not AgentKind.FORK
        ):
            raise ValueError("Only fork sessions may use a parent snapshot")
        if agent_kind is AgentKind.FORK and context_origin is ContextOrigin.FRESH:
            raise ValueError("Fork sessions require a parent snapshot or resume history")
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
        agent_depth = parent.agent_depth.child() if parent is not None else AgentDepth()
        now = _utc_now()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    id, parent_id, root_id, agent_name, mode, title, status,
                    repo_path, summary, error, metadata_json, agent_kind,
                    context_origin, execution_placement, workspace_mode,
                    agent_depth, run_generation, created_at, updated_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL)
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
                    agent_kind.value,
                    context_origin.value,
                    execution_placement.value,
                    workspace_mode.value,
                    agent_depth.value,
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
                WHERE agent_result_json IS NOT NULL
                ORDER BY created_at, id
                """
            ).fetchall()
        records = [self._row_to_session(row) for row in rows]
        return [
            record for record in records
            if (
                record.agent_result is not None
                and record.agent_result.worktree_disposition in dispositions
            )
        ]

    def list_sessions(
        self, limit: int = 50, offset: int = 0,
    ) -> list[SessionRecord]:
        """List all sessions ordered by most recently updated, with pagination.

        Args:
            limit: Maximum number of sessions to return (default 50).
            offset: Number of sessions to skip (default 0).

        Returns:
            list[SessionRecord]: Ordered by ``updated_at`` descending.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._row_to_session(row) for row in rows]

    # ── Context budget: tool output cap ───────────────────────────────────
    _MAX_TOOL_OUTPUT_CHARS: int = 2_000
    _MAX_INTERMEDIATE_ASSISTANT_CHARS: int = 500
    # P2: Legacy budget — ContextWindowManager (v2 default) handles actual
    # trimming.  This ceiling is now a generous safety net, not the primary
    # budget.  Set high enough to avoid conflicting with the v2 manager.
    _CONTEXT_TOKEN_BUDGET: int = 200_000

    def append_message(self, session_id: str, message: LLMMessage) -> None:
        """Persist a message — full content, no truncation.

        Truncation is applied at READ time (``list_messages_for_context()``)
        so the frontend always sees complete messages via ``list_messages()``.
        """
        if self.get_session(session_id) is None:
            raise ValueError(f"Unknown session: {session_id}")
        # Skip Runtime-only messages that should never appear in the frontend
        if message.kind in (MessageKind.RUNTIME_NOTICE, MessageKind.PLAN_CONTEXT):
            return
        tool_name = None
        tool_calls_json = None
        if message.tool_calls:
            tool_calls_json = json.dumps(
                [tool_call.to_dict() for tool_call in message.tool_calls],
                ensure_ascii=True,
            )
            tool_name = ",".join(tc.name for tc in message.tool_calls)
        from agent.session.message_serializer import (
            content_to_json, content_to_text, infer_message_kind,
        )
        content_json_str = content_to_json(message.content)
        content_text = content_to_text(message.content)
        content_legacy = str(message.content)
        _kind = infer_message_kind(
            message.role,
            getattr(message, "tool_call_id", None),
            getattr(message, "tool_calls", None),
        )
        _turn_id = getattr(message, "turn_id", "") or ""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO session_messages (
                    session_id, role, content, content_json, message_kind,
                    tool_call_id, tool_name,
                    tool_calls_json, turn_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    message.role,
                    content_legacy,
                    content_json_str,
                    _kind.value,
                    message.tool_call_id,
                    tool_name,
                    tool_calls_json,
                    _turn_id,
                    _utc_now(),
                ),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (_utc_now(), session_id),
            )

    def replace_messages_with_compaction(
        self,
        session_id: str,
        messages: list[dict],
        *,
        method: str = "snip_micro_auto",
        tokens_before: int = 0,
        tokens_after: int = 0,
        truncation_status: str = "",
    ) -> dict:
        """Atomically archive active messages and replace them with compacted ones."""
        if self.get_session(session_id) is None:
            raise ValueError(f"Unknown session: {session_id}")
        import hashlib

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM session_messages WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
            source_ids = [int(row["id"]) for row in rows]
            source_hash = hashlib.sha256(
                json.dumps(source_ids, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            compacted_json = json.dumps(messages, ensure_ascii=False, sort_keys=True)
            summary_hash = hashlib.sha256(compacted_json.encode("utf-8")).hexdigest()
            existing = conn.execute(
                """SELECT * FROM compaction_runs
                   WHERE session_id=? AND status='completed'
                     AND (source_hash=? OR summary_hash=?)
                   ORDER BY created_at DESC LIMIT 1""",
                (session_id, source_hash, summary_hash),
            ).fetchone()
            if existing is not None:
                conn.execute("COMMIT")
                return dict(existing) | {"action": "NOOP"}

            compaction_id = uuid.uuid4().hex
            now = _utc_now()
            conn.execute(
                """INSERT INTO compaction_runs
                   (id, session_id, source_hash, source_message_ids_json, summary_hash,
                    method, tokens_before, tokens_after, truncation_status, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)""",
                (compaction_id, session_id, source_hash, json.dumps(source_ids),
                 summary_hash, method, int(tokens_before), int(tokens_after),
                 truncation_status, now),
            )
            for row in rows:
                conn.execute(
                    """INSERT INTO session_message_archive
                       (compaction_id, original_message_id, session_id, role, content,
                        tool_call_id, tool_name, tool_calls_json, turn_id, created_at, archived_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (compaction_id, row["id"], session_id, row["role"], row["content"],
                     row["tool_call_id"], row["tool_name"], row["tool_calls_json"],
                     row["turn_id"], row["created_at"], now),
                )
            conn.execute("DELETE FROM session_messages WHERE session_id=?", (session_id,))
            for message in messages:
                tool_calls = message.get("tool_calls") or None
                tool_calls_json = (
                    json.dumps(tool_calls, ensure_ascii=True) if tool_calls else None
                )
                tool_name = ",".join(
                    str(item.get("name", "")) for item in tool_calls or []
                ) or None
                conn.execute(
                    """INSERT INTO session_messages
                       (session_id, role, content, tool_call_id, tool_name,
                        tool_calls_json, turn_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (session_id, str(message.get("role", "user")),
                     str(message.get("content", "")), message.get("tool_call_id"),
                     tool_name, tool_calls_json, str(message.get("turn_id", "")), now),
                )
            conn.execute(
                """UPDATE compaction_runs
                   SET status='completed', completed_at=? WHERE id=?""",
                (now, compaction_id),
            )
            conn.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?",
                (now, session_id),
            )
            conn.execute("COMMIT")
        return {
            "id": compaction_id, "session_id": session_id, "action": "COMPACTED",
            "source_count": len(rows), "active_count": len(messages),
            "source_hash": source_hash, "summary_hash": summary_hash,
            "tokens_before": int(tokens_before), "tokens_after": int(tokens_after),
        }

    def list_compaction_runs(self, session_id: str) -> list[dict]:
        with self._connect() as conn:
            return [
                dict(row) for row in conn.execute(
                    "SELECT * FROM compaction_runs WHERE session_id=? ORDER BY created_at DESC",
                    (session_id,),
                ).fetchall()
            ]

    def list_archived_messages(self, session_id: str) -> list[dict]:
        with self._connect() as conn:
            return [
                dict(row) for row in conn.execute(
                    """SELECT * FROM session_message_archive
                       WHERE session_id=? ORDER BY original_message_id""",
                    (session_id,),
                ).fetchall()
            ]

    # Runtime-injected prompt-engineering messages start with these
    # prefixes — they should never appear in the frontend.
    _RUNTIME_PREFIXES: tuple[str, ...] = (
        "[TASK ANCHOR]", "[ENVIRONMENT]", "[PRELOADED SKILLS]",
        "[AGENT MEMORY]", "[TASK MODE]", "[ACTIVE POLICY]",
        "[FEEDBACK]", "[PREVIOUS SESSION CONTEXT]", "[SYSTEM]",
        "[MEMORY RESTORED]", "[ACCUMULATED FINDINGS]", "[PLAN CONTEXT]",
        "[Conversation compacted", "[Earlier conversation summarized",
    )

    def list_messages(self, session_id: str) -> list[LLMMessage]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, role, content, content_json, message_kind,
                       tool_call_id, tool_name,
                       tool_calls_json, turn_id, created_at
                FROM session_messages
                WHERE session_id = ?
                ORDER BY id
                """,
                (session_id,),
            ).fetchall()
        result: list[LLMMessage] = []
        for row in rows:
            # P0_4: filter by message_kind (precise) first; fall back to prefix (legacy)
            _kind_str = row["message_kind"] if "message_kind" in row.keys() else None
            if _kind_str and _kind_str in ("system", "runtime_notice", "plan_context"):
                continue
            if not _kind_str:
                content = row["content"] or ""
                if any(content.startswith(p) for p in self._RUNTIME_PREFIXES):
                    continue
            tool_calls = None
            raw_tool_calls = row["tool_calls_json"]
            if raw_tool_calls:
                tool_calls = [
                    ToolCall(name=tc["name"], params=tc["params"], id=tc.get("id"))
                    for tc in json.loads(raw_tool_calls)
                ]
            # P0_4: restore from content_json if available, fall back to content text
            from agent.session.message_serializer import (
                collapse_plain_text_content,
                content_from_json,
            )
            restored_content = collapse_plain_text_content(content_from_json(
                row["content_json"] if "content_json" in row.keys() else None,
                fallback_text=row["content"] or "",
            ))
            # P0_4: restore correct message kind
            if _kind_str:
                try:
                    restored_kind = MessageKind(_kind_str)
                except ValueError:
                    restored_kind = MessageKind.USER if row["role"] == "user" else MessageKind.ASSISTANT
            else:
                restored_kind = MessageKind.USER if row["role"] == "user" else MessageKind.ASSISTANT

            result.append(LLMMessage(
                role=row["role"],
                content=restored_content,
                tool_call_id=row["tool_call_id"],
                tool_calls=tool_calls,
                kind=restored_kind,
                created_at=row["created_at"],
            ))
            # Attach DB id for incremental reload (subagent S4: live steering)
            result[-1].db_id = row["id"]  # type: ignore[attr-defined]
            # Preserve durable turn ownership for timeline reconstruction.
            # append_message() stores this column, so dropping it on read
            # makes every refreshed message appear ungrouped.
            result[-1].turn_id = row["turn_id"] or ""  # type: ignore[attr-defined]
        return result

    def list_messages_for_context(self, session_id: str) -> list[LLMMessage]:
        """Load messages with context-budget truncation for LLM injection.

        This is the **read-time truncation** counterpart of ``list_messages()``.
        ``list_messages()`` returns full content for frontend display;
        this method returns budget-capped content for multi-turn LLM context.

        Three caps:
        1. tool outputs > 2000 chars → truncated (Read/Bash/etc. raw output)
        2. intermediate assistant thoughts > 500 chars → truncated (pre-action reasoning)
        3. 8000-token ceiling — keep first message + fill from most-recent-first
        """
        full = self.list_messages(session_id)
        if not full:
            return full

        # Older Web runs could persist the same prompt in both the atomic
        # Run/Turn submission and SessionRuntime. Hide that legacy duplication
        # from model context without rewriting the auditable message history.
        # A genuine repeated turn has an assistant/tool message between user
        # messages, so only adjacent byte-identical user rows are collapsed.
        deduped: list[LLMMessage] = []
        for msg in full:
            if (
                deduped
                and msg.role == "user"
                and deduped[-1].role == "user"
                and str(msg.content or "") == str(deduped[-1].content or "")
            ):
                continue
            deduped.append(msg)
        full = deduped

        # ── Cap 1+2: per-message truncation ──
        capped: list[LLMMessage] = []
        for msg in full:
            content = str(msg.content or "")
            if msg.role == "tool" and len(content) > self._MAX_TOOL_OUTPUT_CHARS:
                msg = LLMMessage(
                    role=msg.role, tool_call_id=msg.tool_call_id,
                    content=(
                        content[:self._MAX_TOOL_OUTPUT_CHARS]
                        + f"\n…[truncated — {len(content) - self._MAX_TOOL_OUTPUT_CHARS} more chars]"
                    ),
                )
            elif (msg.role == "assistant" and msg.tool_calls
                  and len(content) > self._MAX_INTERMEDIATE_ASSISTANT_CHARS):
                msg = LLMMessage(
                    role=msg.role, tool_calls=msg.tool_calls,
                    content=(
                        content[:self._MAX_INTERMEDIATE_ASSISTANT_CHARS]
                        + "\n…[intermediate thought truncated]"
                    ),
                )
            capped.append(msg)

        # ── Cap 3: token budget — keep first + most recent ──
        _token_est = lambda m: max(1, len(str(m.content or "")) // 3)
        result: list[LLMMessage] = [capped[0]]
        remaining = self._CONTEXT_TOKEN_BUDGET - _token_est(capped[0])
        recent: list[LLMMessage] = []
        for msg in reversed(capped[1:]):
            cost = _token_est(msg)
            if cost > remaining:
                break
            recent.append(msg)
            remaining -= cost
        result.extend(reversed(recent))
        return result

    def append_agent_notification(
        self, notification: AgentCompletionNotification,
    ) -> None:
        """Persist one terminal child result exactly once."""
        if not isinstance(notification, AgentCompletionNotification):
            raise TypeError("notification must be an AgentCompletionNotification")
        parent = self.get_session(notification.parent_session_id)
        child = self.get_session(notification.child_session_id)
        if parent is None or child is None or child.parent_id != parent.id:
            raise ValueError("Completion notification must identify a direct child")
        payload = json.dumps(notification.to_dict(), ensure_ascii=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO agent_notifications (
                    parent_session_id, child_session_id, generation, payload_json,
                    delivery_state, created_at, delivered_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    notification.parent_session_id,
                    notification.child_session_id,
                    notification.generation,
                    payload,
                    NotificationDeliveryState.PENDING.value,
                    _utc_now(),
                ),
            )

    def create_delegation_run(
        self,
        *,
        run_id: str,
        parent_session_id: str,
        topology: str,
        reason_code: str = "",
        explanation: str = "",
        parent_run_id: str = "",
        budget: dict[str, object] | None = None,
        downgraded_from: str | None = None,
        is_team: bool = False,
    ) -> dict[str, object]:
        if self.get_session(parent_session_id) is None:
            raise ValueError(f"Unknown parent session: {parent_session_id}")
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO delegation_runs (
                    id, parent_session_id, parent_run_id, topology,
                    reason_code, explanation, status, budget_json,
                    downgraded_from, is_team, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, NULL)
                """,
                (
                    run_id,
                    parent_session_id,
                    parent_run_id,
                    topology,
                    reason_code,
                    explanation,
                    json.dumps(budget or {}, ensure_ascii=True),
                    downgraded_from,
                    int(is_team),
                    now,
                ),
            )
        return self.get_delegation_run(run_id) or {}

    def create_delegation_task(
        self,
        *,
        task_id: str,
        delegation_run_id: str,
        agent_type: str,
        goal: str,
        prompt: str = "",
        purpose: str = "analysis",
        scope: tuple[str, ...] = (),
        dependencies: tuple[str, ...] = (),
        expected_files: tuple[str, ...] = (),
        write_files: tuple[str, ...] = (),
        required: bool = True,
        retry_count: int = 0,
        max_retries: int = 1,
        supersedes_task_id: str | None = None,
    ) -> dict[str, object]:
        if retry_count < 0 or max_retries < 0 or retry_count > max_retries:
            raise ValueError("Invalid delegation retry budget")
        now = _utc_now()
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM delegation_runs WHERE id = ?",
                (delegation_run_id,),
            ).fetchone()
            if exists is None:
                raise ValueError(f"Unknown delegation run: {delegation_run_id}")
            if supersedes_task_id is not None:
                cursor = conn.execute(
                    """
                    UPDATE delegation_tasks
                    SET status = 'superseded',
                        completed_at = COALESCE(completed_at, ?)
                    WHERE id = ? AND delegation_run_id = ?
                      AND status IN (
                          'failed', 'cancelled', 'interrupted',
                          'partial', 'budget_exhausted'
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM delegation_tasks
                          WHERE supersedes_task_id = ?
                      )
                    """,
                    (
                        now,
                        supersedes_task_id,
                        delegation_run_id,
                        supersedes_task_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        "The delegation task is not retryable or was already superseded"
                    )
            conn.execute(
                """
                INSERT INTO delegation_tasks (
                    id, delegation_run_id, child_session_id, generation,
                    agent_type, purpose, goal, prompt, scope_json,
                    dependencies_json, expected_files_json, write_files_json,
                    required, status, report_json, error, retry_count,
                    max_retries, supersedes_task_id, integration_status,
                    integration_error, created_at, started_at, completed_at
                ) VALUES (?, ?, NULL, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued',
                          NULL, '', ?, ?, ?, 'not_required', '', ?, NULL, NULL)
                """,
                (
                    task_id,
                    delegation_run_id,
                    agent_type,
                    purpose,
                    goal,
                    prompt,
                    json.dumps(list(scope), ensure_ascii=True),
                    json.dumps(list(dependencies), ensure_ascii=True),
                    json.dumps(list(expected_files), ensure_ascii=True),
                    json.dumps(list(write_files), ensure_ascii=True),
                    int(required),
                    retry_count,
                    max_retries,
                    supersedes_task_id,
                    now,
                ),
            )
            if supersedes_task_id is not None:
                conn.execute(
                    """
                    UPDATE delegation_runs
                    SET status = 'running', phase = 'executing',
                        completed_at = NULL, version = version + 1
                    WHERE id = ?
                    """,
                    (delegation_run_id,),
                )
        return self.get_delegation_task(task_id) or {}

    def prepare_delegation_retry(
        self, task_id: str,
    ) -> list[dict[str, object]]:
        task = self.get_delegation_task(task_id)
        if task is None:
            raise ValueError(f"Unknown delegation task: {task_id}")
        if str(task["status"]) not in {
            "failed", "cancelled", "interrupted", "partial", "budget_exhausted",
        }:
            raise ValueError("Only a terminal incomplete task can be retried")
        return self._replace_delegation_subgraph(
            str(task["delegation_run_id"]), {task_id},
        )

    def prepare_delegation_resume(
        self, run_id: str,
    ) -> list[dict[str, object]]:
        run = self.get_delegation_run(run_id)
        if run is None:
            raise ValueError(f"Unknown delegation run: {run_id}")
        tasks = self.list_delegation_tasks(run_id)
        seeds = {
            str(task["id"])
            for task in tasks
            if str(task["status"]) == "interrupted"
        }
        if not seeds:
            raise ValueError("Delegation run has no interrupted tasks to resume")
        return self._replace_delegation_subgraph(run_id, seeds)

    def _replace_delegation_subgraph(
        self, run_id: str, seed_ids: set[str],
    ) -> list[dict[str, object]]:
        """Atomically supersede seeds and every downstream consumer."""
        now = _utc_now()
        replacement_ids: list[str] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                "SELECT * FROM delegation_runs WHERE id = ?", (run_id,),
            ).fetchone()
            if run is None:
                raise ValueError(f"Unknown delegation run: {run_id}")
            rows = conn.execute(
                """
                SELECT * FROM delegation_tasks
                WHERE delegation_run_id = ? AND status != 'superseded'
                ORDER BY created_at, id
                """,
                (run_id,),
            ).fetchall()
            by_id = {str(row["id"]): row for row in rows}
            if not seed_ids or not seed_ids <= set(by_id):
                raise ValueError("Retry seed is not an effective task in this run")
            affected = set(seed_ids)
            changed = True
            while changed:
                changed = False
                for task_id, row in by_id.items():
                    dependencies = {
                        str(item)
                        for item in json.loads(row["dependencies_json"] or "[]")
                    }
                    if task_id not in affected and dependencies & affected:
                        affected.add(task_id)
                        changed = True
            unresolved_worktrees = [
                task_id for task_id in affected
                if str(by_id[task_id]["integration_status"]) in {"pending", "retained"}
            ]
            if unresolved_worktrees:
                raise ValueError(
                    "Resolve affected worktrees before retrying: "
                    + ", ".join(sorted(unresolved_worktrees))
                )
            mapping: dict[str, str] = {}
            for task_id in sorted(affected):
                row = by_id[task_id]
                retry_count = int(row["retry_count"]) + 1
                if retry_count > int(row["max_retries"]):
                    raise ValueError(f"Task retry budget exhausted: {task_id}")
                mapping[task_id] = (
                    f"{task_id}:generation-{retry_count}-{uuid.uuid4().hex[:8]}"
                )
            remaining = set(affected)
            ordered: list[str] = []
            while remaining:
                ready = sorted(
                    task_id for task_id in remaining
                    if not (
                        {
                            str(item)
                            for item in json.loads(
                                by_id[task_id]["dependencies_json"] or "[]"
                            )
                        }
                        & remaining
                    )
                )
                if not ready:
                    raise ValueError("Delegation task graph contains a cycle")
                ordered.extend(ready)
                remaining.difference_update(ready)
            for old_id in ordered:
                row = by_id[old_id]
                existing = conn.execute(
                    "SELECT 1 FROM delegation_tasks WHERE supersedes_task_id = ?",
                    (old_id,),
                ).fetchone()
                if existing is not None:
                    raise ValueError(f"Task was already superseded: {old_id}")
                cursor = conn.execute(
                    """
                    UPDATE delegation_tasks
                    SET status = 'superseded', completed_at = COALESCE(completed_at, ?)
                    WHERE id = ? AND status != 'superseded'
                    """,
                    (now, old_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"Could not claim task for retry: {old_id}")
                old_dependencies = [
                    str(item)
                    for item in json.loads(row["dependencies_json"] or "[]")
                ]
                dependencies = [mapping.get(item, item) for item in old_dependencies]
                new_id = mapping[old_id]
                conn.execute(
                    """
                    INSERT INTO delegation_tasks (
                        id, delegation_run_id, child_session_id, generation,
                        agent_type, purpose, goal, prompt, scope_json,
                        dependencies_json, expected_files_json, write_files_json,
                        required, status, report_json, error, retry_count,
                        max_retries, supersedes_task_id, integration_status,
                        integration_error, created_at, started_at, completed_at
                    ) VALUES (?, ?, NULL, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued',
                              NULL, '', ?, ?, ?, 'not_required', '', ?, NULL, NULL)
                    """,
                    (
                        new_id,
                        run_id,
                        row["agent_type"],
                        row["purpose"],
                        row["goal"],
                        row["prompt"],
                        row["scope_json"],
                        json.dumps(dependencies, ensure_ascii=True),
                        row["expected_files_json"],
                        row["write_files_json"],
                        row["required"],
                        int(row["retry_count"]) + 1,
                        row["max_retries"],
                        old_id,
                        now,
                    ),
                )
                replacement_ids.append(new_id)
            conn.execute(
                """
                UPDATE delegation_runs
                SET status = 'running', phase = 'executing', completed_at = NULL,
                    interrupted_at = NULL, synthesis_json = NULL,
                    verification_json = NULL, version = version + 1
                WHERE id = ?
                """,
                (run_id,),
            )
            conn.execute("COMMIT")
        return [
            task for task_id in replacement_ids
            if (task := self.get_delegation_task(task_id)) is not None
        ]

    def update_delegation_task(
        self,
        task_id: str,
        *,
        status: str,
        child_session_id: str | None = None,
        generation: int | None = None,
        report: dict[str, object] | None = None,
        error: str = "",
        integration_status: str | None = None,
        integration_error: str = "",
        expected_statuses: tuple[str, ...] | None = None,
    ) -> bool:
        now = _utc_now()
        terminal = status in {
            "completed", "partial", "failed", "cancelled", "interrupted",
            "no_findings", "budget_exhausted", "rejected", "superseded",
        }
        where = "id = ?"
        params: list[object] = [
            status,
            child_session_id,
            generation,
            json.dumps(report, ensure_ascii=True) if report is not None else None,
            error,
            integration_status,
            integration_error,
            status,
            now,
            int(terminal),
            now,
            task_id,
        ]
        if expected_statuses:
            placeholders = ", ".join("?" for _ in expected_statuses)
            where += f" AND status IN ({placeholders})"
            params.extend(expected_statuses)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE delegation_tasks
                SET status = ?,
                    child_session_id = COALESCE(?, child_session_id),
                    generation = COALESCE(?, generation),
                    report_json = COALESCE(?, report_json),
                    error = ?,
                    integration_status = COALESCE(?, integration_status),
                    integration_error = ?,
                    started_at = CASE
                        WHEN ? = 'running' THEN COALESCE(started_at, ?)
                        ELSE started_at
                    END,
                    completed_at = CASE WHEN ? THEN ? ELSE completed_at END
                WHERE {where}
                """,
                tuple(params),
            )
        if cursor.rowcount == 1:
            return True
        if self.get_delegation_task(task_id) is None:
            raise ValueError(f"Unknown delegation task: {task_id}")
        return False

    def update_delegation_task_resource(
        self,
        task_id: str,
        resource: dict[str, object],
    ) -> bool:
        """Merge durable resource lifecycle facts for one delegation task."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT resource_json FROM delegation_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return False
            current = (
                json.loads(row["resource_json"])
                if row["resource_json"] else {}
            )
            for key, value in resource.items():
                if isinstance(value, dict) and isinstance(current.get(key), dict):
                    current[key] = {**current[key], **value}
                else:
                    current[key] = value
            cursor = conn.execute(
                """
                UPDATE delegation_tasks
                SET resource_json = ?
                WHERE id = ?
                """,
                (json.dumps(current, ensure_ascii=True), task_id),
            )
        return cursor.rowcount == 1

    def transition_delegation_run(
        self,
        run_id: str,
        *,
        status: str,
        phase: str,
        expected_statuses: tuple[str, ...] | None = None,
        expected_version: int | None = None,
        synthesis: dict[str, object] | None = None,
        verification: dict[str, object] | None = None,
    ) -> bool:
        terminal = status in {"completed", "partial", "failed", "cancelled"}
        now = _utc_now()
        where = "id = ?"
        params: list[object] = [
            status,
            phase,
            json.dumps(synthesis, ensure_ascii=True) if synthesis is not None else None,
            json.dumps(verification, ensure_ascii=True) if verification is not None else None,
            now if terminal else None,
            run_id,
        ]
        if expected_statuses:
            placeholders = ", ".join("?" for _ in expected_statuses)
            where += f" AND status IN ({placeholders})"
            params.extend(expected_statuses)
        if expected_version is not None:
            where += " AND version = ?"
            params.append(expected_version)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE delegation_runs
                SET status = ?, phase = ?,
                    synthesis_json = COALESCE(?, synthesis_json),
                    verification_json = COALESCE(?, verification_json),
                    completed_at = ?, version = version + 1
                WHERE {where}
                """,
                tuple(params),
            )
        return cursor.rowcount == 1

    def finalize_delegation_run(
        self,
        run_id: str,
        *,
        status: str,
        phase: str,
        expected_version: int,
        report_count: int = 0,
        verification: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        """Atomically CAS a delegation terminal state and append its outbox fact.

        A successful call is the only path that creates ``delegation_completed``.
        Concurrent/stale callers lose the CAS and therefore cannot persist or
        broadcast a duplicate terminal event. A retried run has a newer version
        and may later produce one terminal event for that new execution.
        """
        if status not in {"completed", "partial", "failed", "cancelled"}:
            raise ValueError(f"Invalid delegation terminal status: {status}")
        from server.services.event_outbox import OutboxStore

        outbox = OutboxStore(self._db_path)
        outbox.install()
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM delegation_runs WHERE id = ?", (run_id,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise ValueError(f"Unknown delegation run: {run_id}")
            if int(row["version"]) != expected_version or str(row["status"]) in {
                "completed", "partial", "failed", "cancelled",
            }:
                conn.execute("ROLLBACK")
                return None
            next_version = expected_version + 1
            cursor = conn.execute(
                """
                UPDATE delegation_runs
                SET status = ?, phase = ?,
                    verification_json = COALESCE(?, verification_json),
                    completed_at = ?, version = ?
                WHERE id = ? AND version = ?
                  AND status NOT IN ('completed', 'partial', 'failed', 'cancelled')
                """,
                (
                    status,
                    phase,
                    json.dumps(verification, ensure_ascii=True)
                    if verification is not None else None,
                    now,
                    next_version,
                    run_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            event = {
                "type": "delegation_completed",
                "session_id": str(row["parent_session_id"]),
                "run_id": str(row["parent_run_id"]),
                "delegation_run_id": run_id,
                "event_id": f"delegation-terminal:{run_id}:{next_version}",
                "timestamp": now,
                "status": status,
                "phase": phase,
                "report_count": report_count,
                "version": next_version,
            }
            outbox.append_event(
                conn,
                event["event_id"],
                "delegation.completed",
                str(row["parent_session_id"]),
                run_id,
                next_version,
                json.dumps(event, ensure_ascii=False),
            )
            conn.execute("COMMIT")
            return event

    def complete_delegation_run(self, run_id: str, *, status: str) -> None:
        """Finalize a delegation through the atomic terminal-event path.

        Kept as a compatibility helper for team and legacy callers. New
        orchestration code should use ``reconcile_delegation_run`` when task
        facts determine the outcome.
        """
        current = self.get_delegation_run(run_id)
        if current is None:
            raise ValueError(f"Unknown delegation run: {run_id}")
        if str(current["status"]) in {"completed", "partial", "failed", "cancelled"}:
            return
        phase = "completed" if status == "completed" else status
        terminal_event = self.finalize_delegation_run(
            run_id,
            status=status,
            phase=phase,
            expected_version=int(current["version"]),
            report_count=len(self.list_delegation_tasks(run_id)),
        )
        if terminal_event is None:
            latest = self.get_delegation_run(run_id)
            if latest is None or str(latest["status"]) not in {
                "completed", "partial", "failed", "cancelled",
            }:
                raise ValueError(f"Delegation run changed before finalization: {run_id}")

    def reconcile_delegation_run(self, run_id: str) -> dict[str, object]:
        current = self.get_delegation_run(run_id)
        if current is None:
            raise ValueError(f"Unknown delegation run: {run_id}")
        if str(current["status"]) == "cancelled":
            return current
        tasks = self.list_delegation_tasks(run_id)
        if not tasks:
            terminal_event = self.finalize_delegation_run(
                run_id,
                status="failed",
                phase="failed",
                expected_version=int(current["version"]),
            )
            failed = self.get_delegation_run(run_id) or {}
            if terminal_event is not None:
                failed["_terminal_event"] = terminal_event
            return failed
        effective = [task for task in tasks if task["status"] != "superseded"]
        terminal = {
            "completed", "partial", "failed", "cancelled", "interrupted",
            "no_findings", "budget_exhausted", "rejected",
        }
        required_failures = [
            task for task in effective
            if bool(task["required"])
            and str(task["status"]) not in {"completed", "no_findings"}
            and str(task["status"]) in terminal
        ]
        pending = [task for task in effective if str(task["status"]) not in terminal]
        awaiting_integration = [
            task for task in effective
            if str(task["integration_status"]) in {"pending", "applying"}
        ]
        rejected_integration = [
            task for task in effective
            if bool(task["required"])
            and str(task["integration_status"]) in {
                "discarded", "retained", "conflict", "stale",
                "contract_violation",
            }
        ]
        integrated_changes = any(
            str(task["integration_status"]) == "applied"
            for task in effective
        )
        verification = (
            current.get("verification")
            if isinstance(current.get("verification"), dict) else {}
        )
        verification_status = str(verification.get("status", ""))
        if pending:
            status, phase = "running", "executing"
        elif awaiting_integration:
            status, phase = "running", "awaiting_integration"
        elif required_failures or rejected_integration:
            status, phase = "partial", "partial"
        elif integrated_changes and verification_status != "passed":
            if verification_status == "failed":
                status, phase = "partial", "verification_failed"
            else:
                status, phase = "running", "awaiting_verification"
        else:
            status, phase = "completed", "completed"
        if status in {"completed", "partial", "failed", "cancelled"}:
            terminal_event = self.finalize_delegation_run(
                run_id,
                status=status,
                phase=phase,
                expected_version=int(current["version"]),
                report_count=sum(
                    str(task["status"]) in terminal for task in effective
                ),
            )
            converged = self.get_delegation_run(run_id) or {}
            if terminal_event is not None:
                converged["_terminal_event"] = terminal_event
            return converged
        changed = self.transition_delegation_run(
            run_id,
            status=status,
            phase=phase,
            expected_version=int(current["version"]),
        )
        if not changed:
            return self.get_delegation_run(run_id) or {}
        return self.get_delegation_run(run_id) or {}

    def reconcile_interrupted_delegations(self) -> list[str]:
        """Converge non-team in-flight work after process restart."""
        now = _utc_now()
        interrupted: list[str] = []
        stable: list[str] = []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM delegation_runs
                WHERE is_team = 0 AND status = 'running'
                """
            ).fetchall()
            for row in rows:
                run_id = str(row["id"])
                cursor = conn.execute(
                    """
                    UPDATE delegation_tasks
                    SET status = 'interrupted', error = 'Runtime restarted',
                        completed_at = COALESCE(completed_at, ?)
                    WHERE delegation_run_id = ? AND status IN ('queued', 'running')
                    """,
                    (now, run_id),
                )
                if cursor.rowcount:
                    interrupted.append(run_id)
                    conn.execute(
                        """
                        UPDATE delegation_runs
                        SET status = 'partial', phase = 'recovery_required',
                            interrupted_at = ?, completed_at = ?,
                            version = version + 1
                        WHERE id = ? AND status = 'running'
                        """,
                        (now, now, run_id),
                    )
                else:
                    stable.append(run_id)
        for run_id in stable:
            self.reconcile_delegation_run(run_id)
        return interrupted

    def get_delegation_run(self, run_id: str) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM delegation_runs WHERE id = ?", (run_id,),
            ).fetchone()
        return self._delegation_run_row(row) if row is not None else None

    def get_delegation_task(self, task_id: str) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM delegation_tasks WHERE id = ?", (task_id,),
            ).fetchone()
        return self._delegation_task_row(row) if row is not None else None

    def get_delegation_task_for_child(
        self, child_session_id: str,
    ) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM delegation_tasks
                WHERE child_session_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (child_session_id,),
            ).fetchone()
        return self._delegation_task_row(row) if row is not None else None

    def list_delegation_runs(
        self, parent_session_id: str,
    ) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM delegation_runs
                WHERE parent_session_id = ?
                ORDER BY created_at, id
                """,
                (parent_session_id,),
            ).fetchall()
        return [self._delegation_run_row(row) for row in rows]

    def list_delegation_tasks(
        self, delegation_run_id: str,
    ) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM delegation_tasks
                WHERE delegation_run_id = ?
                ORDER BY created_at, id
                """,
                (delegation_run_id,),
            ).fetchall()
        return [self._delegation_task_row(row) for row in rows]

    @staticmethod
    def _delegation_run_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "id": str(row["id"]),
            "parent_session_id": str(row["parent_session_id"]),
            "parent_run_id": str(row["parent_run_id"]),
            "topology": str(row["topology"]),
            "reason_code": str(row["reason_code"]),
            "explanation": str(row["explanation"]),
            "status": str(row["status"]),
            "phase": str(row["phase"]),
            "budget": json.loads(row["budget_json"] or "{}"),
            "synthesis": (
                json.loads(row["synthesis_json"])
                if row["synthesis_json"] is not None else None
            ),
            "verification": (
                json.loads(row["verification_json"])
                if row["verification_json"] is not None else None
            ),
            "downgraded_from": row["downgraded_from"],
            "is_team": bool(row["is_team"]),
            "version": int(row["version"]),
            "created_at": str(row["created_at"]),
            "interrupted_at": row["interrupted_at"],
            "completed_at": row["completed_at"],
        }

    @staticmethod
    def _delegation_task_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "id": str(row["id"]),
            "delegation_run_id": str(row["delegation_run_id"]),
            "child_session_id": row["child_session_id"],
            "generation": int(row["generation"]),
            "agent_type": str(row["agent_type"]),
            "purpose": str(row["purpose"]),
            "goal": str(row["goal"]),
            "prompt": str(row["prompt"]),
            "scope": json.loads(row["scope_json"] or "[]"),
            "dependencies": json.loads(row["dependencies_json"] or "[]"),
            "expected_files": json.loads(row["expected_files_json"] or "[]"),
            "write_files": json.loads(row["write_files_json"] or "[]"),
            "required": bool(row["required"]),
            "status": str(row["status"]),
            "report": (
                json.loads(row["report_json"])
                if row["report_json"] is not None else None
            ),
            "error": str(row["error"]),
            "retry_count": int(row["retry_count"]),
            "max_retries": int(row["max_retries"]),
            "supersedes_task_id": row["supersedes_task_id"],
            "integration_status": str(row["integration_status"]),
            "integration_error": str(row["integration_error"]),
            "resource": (
                json.loads(row["resource_json"])
                if row["resource_json"] is not None else {}
            ),
            "created_at": str(row["created_at"]),
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
        }

    # ── Evidence persistence ────────────────────────────────────────────

    def create_evidence(
        self, entry: "EvidenceEntry",
    ) -> "EvidenceEntry":
        """Atomically insert-or-get the canonical evidence row.

        Sequence allocation and idempotency are owned by SQLite so concurrent
        producers cannot create divergent in-memory/database evidence IDs.
        """
        import json as _json
        from agent.session.run_evidence import EvidenceEntry

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT * FROM run_evidence
                WHERE root_run_id = ? AND idempotency_key = ?
                """,
                (entry.root_run_id, entry.idempotency_key),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return EvidenceEntry.from_dict(dict(existing))

            next_sequence = int(conn.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) + 1
                FROM run_evidence WHERE root_run_id = ?
                """,
                (entry.root_run_id,),
            ).fetchone()[0])
            conn.execute(
                """
                INSERT INTO run_evidence (
                    evidence_id, idempotency_key, root_run_id, root_session_id,
                    session_id, producer_session_id, turn_id, kind, status, sequence,
                    schema_version,
                    tool_name, call_id, invocation_id,
                    parameters_digest, result_digest, source_fingerprint,
                    cached, cache_key, path, artifact_id,
                    depends_on_json, parent_evidence_id,
                    summary, metadata_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?
                )
                """,
                (
                    entry.evidence_id, entry.idempotency_key,
                    entry.root_run_id, entry.root_session_id,
                    entry.session_id, entry.producer_session_id, entry.turn_id,
                    entry.kind.value, entry.status.value, next_sequence,
                    entry.schema_version,
                    entry.tool_name, entry.call_id, entry.invocation_id,
                    entry.parameters_digest, entry.result_digest,
                    entry.source_fingerprint,
                    int(entry.cached), entry.cache_key,
                    entry.path, entry.artifact_id,
                    _json.dumps(list(entry.depends_on), ensure_ascii=True),
                    entry.parent_evidence_id,
                    entry.summary,
                    _json.dumps(entry.metadata, ensure_ascii=True, default=str),
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM run_evidence
                WHERE root_run_id = ? AND idempotency_key = ?
                """,
                (entry.root_run_id, entry.idempotency_key),
            ).fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError("evidence insert committed without a canonical row")
        return EvidenceEntry.from_dict(dict(row))

    def list_evidence(
        self, root_run_id: str, *, kind: str | None = None,
    ) -> list[dict[str, object]]:
        """List evidence entries for a root run, optionally filtered by kind."""
        with self._connect() as conn:
            if kind:
                rows = conn.execute(
                    """
                    SELECT * FROM run_evidence
                    WHERE root_run_id = ? AND kind = ?
                    ORDER BY sequence, id
                    """,
                    (root_run_id, kind),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM run_evidence
                    WHERE root_run_id = ?
                    ORDER BY sequence, id
                    """,
                    (root_run_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def delete_run_evidence(self, root_run_id: str) -> None:
        """Delete all evidence for a root run (cascaded on session delete)."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM run_evidence WHERE root_run_id = ?",
                (root_run_id,),
            )

    def list_agent_notifications(
        self, parent_session_id: str,
    ) -> list[dict[str, object]]:
        """Read durable child-completion delivery facts without claiming them."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, parent_session_id, child_session_id, generation,
                       payload_json, delivery_state, created_at, delivered_at
                FROM agent_notifications
                WHERE parent_session_id = ?
                ORDER BY id
                """,
                (parent_session_id,),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "parent_session_id": str(row["parent_session_id"]),
                "child_session_id": str(row["child_session_id"]),
                "generation": int(row["generation"]),
                "payload": json.loads(row["payload_json"]),
                "delivery_state": str(row["delivery_state"]),
                "created_at": str(row["created_at"]),
                "delivered_at": (
                    str(row["delivered_at"])
                    if row["delivered_at"] is not None else None
                ),
            }
            for row in rows
        ]

    def prepare_session_resume(
        self, session_id: str, message: LLMMessage,
    ) -> SessionRecord:
        """Atomically append a prompt and begin a terminal child's next generation."""
        if message.role != "user" or message.tool_calls or message.tool_call_id:
            raise ValueError("A resume message must be a plain user message")
        terminal = tuple(status.value for status in (
            SessionStatus.COMPLETED,
            SessionStatus.PARTIAL,
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
        ))
        now = _utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE sessions
                SET status = ?, context_origin = ?, execution_placement = ?,
                    run_generation = run_generation + 1,
                    summary = '', error = '', agent_result_json = NULL,
                    fork_result_json = NULL, completed_at = NULL, updated_at = ?
                WHERE id = ? AND mode = ?
                  AND status IN ({','.join('?' for _ in terminal)})
                """,
                (
                    SessionStatus.RUNNING.value,
                    ContextOrigin.RESUMED.value,
                    ExecutionPlacement.BACKGROUND.value,
                    now,
                    session_id,
                    SessionMode.SUBAGENT.value,
                    *terminal,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    "Only a terminal subagent session can be resumed"
                )
            conn.execute(
                """
                INSERT INTO session_messages (
                    session_id, role, content, tool_call_id, tool_name,
                    tool_calls_json, created_at
                ) VALUES (?, ?, ?, NULL, NULL, NULL, ?)
                """,
                (session_id, message.role, str(message.content), now),
            )
        resumed = self.get_session(session_id)
        if resumed is None:
            raise ValueError(f"Unknown session: {session_id}")
        return resumed

    def claim_pending_agent_notifications(
        self, parent_session_id: str,
    ) -> tuple[AgentCompletionNotification, ...]:
        """Atomically claim pending child results for one parent session."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id, payload_json
                FROM agent_notifications
                WHERE parent_session_id = ? AND delivery_state = ?
                ORDER BY id
                """,
                (parent_session_id, NotificationDeliveryState.PENDING.value),
            ).fetchall()
            if rows:
                now = _utc_now()
                conn.executemany(
                    """
                    UPDATE agent_notifications
                    SET delivery_state = ?, delivered_at = ?
                    WHERE id = ? AND delivery_state = ?
                    """,
                    [
                        (
                            NotificationDeliveryState.DELIVERED.value,
                            now,
                            row["id"],
                            NotificationDeliveryState.PENDING.value,
                        )
                        for row in rows
                    ],
                )
        return tuple(
            AgentCompletionNotification.from_dict(json.loads(row["payload_json"]))
            for row in rows
        )

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
                raise ValueError(f"Unknown session: {session_id}")

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
                raise ValueError(f"Unknown session: {session_id}")

    # ── Run lifecycle ────────────────────────────────────────────────────

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
        """CAS update a run record. Returns True if a row was updated."""
        try:
            parts = ["updated_at = ?"]
            params: list = [_utc_now()]

            if status is not None:
                parts.append("status = ?")
                params.append(status)
                if status in {"completed", "failed", "cancelled"}:
                    parts.append("completed_at = ?")
                    params.append(_utc_now())
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

            with self._connect() as conn:
                cur = conn.execute(
                    f"UPDATE runs SET {', '.join(parts)} WHERE {where}",
                    params,
                )
                return cur.rowcount == 1
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Failed to update run %s", run_id)
            return False

    def finalize_run_with_event(
        self,
        run_id: str,
        session_id: str,
        *,
        status: str = "completed",
        summary: str = "",
        steps_taken: int = 0,
        total_tokens: int = 0,
        error: str = "",
        event_payload: dict | None = None,
        expect_status: str = "running",
    ) -> bool:
        """R3.3: CAS-update Run + INSERT outbox event in ONE transaction.

        Returns True if CAS succeeded and outbox was written.
        The trace projection will pick up the outbox event separately.
        """
        from server.services.event_outbox import OutboxStore
        import json as _json
        import uuid as _uuid

        event_id = str(_uuid.uuid4())
        event_type = f"run.{status}"
        terminal_payload = {
            "status": status, "summary": summary,
            "steps_taken": steps_taken, "total_tokens": total_tokens,
            "error": error,
        }
        terminal_payload.update(event_payload or {})
        payload = _json.dumps(terminal_payload, ensure_ascii=False)

        try:
            outbox = OutboxStore(self._db_path)
            outbox.install()
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")

                cur = conn.execute(
                    """UPDATE runs SET status = ?, summary = ?, steps_taken = ?,
                       total_tokens = ?, error = ?, termination_reason = ?,
                       verification_status = ?, verification_reason = ?,
                       verification_checks_json = ?, workspace_delta_json = ?,
                       completed_at = ?, updated_at = ?
                       WHERE id = ? AND status = ?""",
                    (
                        status, summary, steps_taken, total_tokens, error,
                        terminal_payload.get("termination_reason", "none"),
                        terminal_payload.get("verification_status", "not_applicable"),
                        terminal_payload.get("verification_reason", "none"),
                        _json.dumps(terminal_payload.get("verification", {}).get("checks", []), ensure_ascii=False),
                        _json.dumps(terminal_payload.get("workspace_delta", {}), ensure_ascii=False),
                        _utc_now(), _utc_now(), run_id, expect_status,
                    ),
                )
                if cur.rowcount != 1:
                    conn.execute("ROLLBACK")
                    return False

                outbox.append_event(
                    conn, event_id, event_type, session_id,
                    run_id, 1, payload,
                )

                conn.execute("COMMIT")
                return True
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "Failed to finalize_run_with_event %s", run_id)
            return False

    def start_run_with_event(
        self,
        run_id: str,
        session_id: str,
        *,
        turn_id: str = "",
        turn_index: int = 0,
    ) -> bool:
        """CAS queued->running and append run.started atomically."""
        # G36M-3: DEPRECATED — use application.events.envelope.EventEnvelope (G3)
        from server.domain_events import DomainEvent  # noqa: G36M
        from server.services.event_outbox import OutboxStore

        outbox = OutboxStore(self._db_path)
        outbox.install()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    "UPDATE runs SET status='running', updated_at=? "
                    "WHERE id=? AND status='queued'",
                    (_utc_now(), run_id),
                )
                if cur.rowcount != 1:
                    conn.execute("ROLLBACK")
                    return False
                outbox.append(conn, DomainEvent(
                    event_type="run.started",
                    session_id=session_id,
                    aggregate_id=run_id,
                    aggregate_version=2,
                    payload={"turn_id": turn_id, "turn_index": turn_index},
                ))
                conn.execute("COMMIT")
                return True
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "Failed to start_run_with_event %s", run_id,
            )
            return False

    def update_metadata(
        self, session_id: str, extra: dict[str, Any]
    ) -> None:
        """Merge *extra* keys into the session's metadata_json."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT metadata_json FROM sessions WHERE id = ?", (session_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown session: {session_id}")
            existing = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
            existing.update(extra)
            conn.execute(
                "UPDATE sessions SET metadata_json = ? WHERE id = ?",
                (json.dumps(existing, ensure_ascii=True), session_id),
            )

    def set_agent_result(self, session_id: str, result: AgentRunResult) -> None:
        """Persist the generic typed child result at the session boundary."""
        payload = json.dumps(result.to_dict(), ensure_ascii=True)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE sessions
                SET agent_result_json = ?, fork_result_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (payload, payload, _utc_now(), session_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown session: {session_id}")

    def set_fork_result(self, session_id: str, result: ForkResult) -> None:
        """Compatibility adapter for execution APIs migrated in Batch 3."""
        self.set_agent_result(session_id, result)

    def touch_session(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (_utc_now(), session_id),
            )

    def _row_to_session(self, row: sqlite3.Row) -> SessionRecord:
        raw_agent_result = row["agent_result_json"] or row["fork_result_json"]
        return SessionRecord(
            id=row["id"],
            parent_id=row["parent_id"],
            root_id=row["root_id"],
            agent_name=row["agent_name"],
            mode=SessionMode(row["mode"]),
            title=row["title"],
            status=SessionStatus(row["status"]),
            repo_path=row["repo_path"],
            agent_kind=AgentKind(row["agent_kind"]),
            context_origin=ContextOrigin(row["context_origin"]),
            execution_placement=ExecutionPlacement(row["execution_placement"]),
            workspace_mode=WorkspaceMode(row["workspace_mode"]),
            agent_depth=AgentDepth(int(row["agent_depth"])),
            generation=int(row["run_generation"]),
            summary=row["summary"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            metadata=json.loads(row["metadata_json"] or "{}"),
            agent_result=(
                AgentRunResult.from_dict(json.loads(raw_agent_result))
                if raw_agent_result else None
            ),
        )
