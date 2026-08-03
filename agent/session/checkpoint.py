"""
CC-Native Step Checkpoint & Idempotent Tool Recovery (P1-1).

CheckpointManager: captures turn-boundary state so crashed sessions
    can resume from the last committed turn.

IdempotentToolCache: prevents duplicate tool execution when replaying
    a partially-completed turn after recovery.

Design: CC-aligned prompt-level checkpoint + tool result dedup.

⚠️ 定位（Phase 3C）：Checkpoint 是**调试工具，不是生产恢复机制**。
默认关闭（AgentConfig.checkpoint_db_path=""，零开销）。生产环境的
恢复依赖 Git（唯一状态源）+ Evidence（turn 级 RESUME_MARKER，
见 run_evidence.py）。仅在调试长任务或验证状态机行为时通过
--enable-checkpoint-debug 开启，避免后续开发者误以为这是功能缺失
而反复尝试启用。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── StepCheckpoint ──────────────────────────────────────────────────────────

@dataclass
class StepCheckpoint:
    session_id: str
    generation: int
    turn_number: int
    file_snapshot_json: str = "{}"
    pending_tool_ids_json: str = "[]"
    tool_results_json: str = "{}"      # invocation_id → serialized ToolResult
    active_skills_json: str = "[]"
    created_at: str = ""


# ── CheckpointManager ───────────────────────────────────────────────────────

class CheckpointManager:
    """Turn-boundary checkpoint capture and restore.

    Checkpoints are stored in the session_store database.
    Each checkpoint captures: turn number, pending tools, completed
    tool results, active skills.
    """

    KEEP_LAST_N = 5

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    # ── schema ──────────────────────────────────────────────────────────

    def ensure_table(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS session_checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    turn_number INTEGER NOT NULL,
                    file_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    pending_tool_ids_json TEXT NOT NULL DEFAULT '[]',
                    tool_results_json TEXT NOT NULL DEFAULT '{}',
                    active_skills_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_checkpoints_session
                ON session_checkpoints(session_id, generation, turn_number)
            """)

    # ── capture ─────────────────────────────────────────────────────────

    def capture(
        self,
        session_id: str,
        generation: int,
        turn_number: int,
        *,
        pending_tool_ids: list[str] | None = None,
        tool_results: dict[str, Any] | None = None,
        active_skills: list[dict] | None = None,
    ) -> StepCheckpoint:
        self.ensure_table()
        cp = StepCheckpoint(
            session_id=session_id,
            generation=generation,
            turn_number=turn_number,
            pending_tool_ids_json=json.dumps(pending_tool_ids or [], ensure_ascii=False),
            tool_results_json=json.dumps(
                {k: _serialize_result(v) for k, v in (tool_results or {}).items()},
                ensure_ascii=False, default=str,
            ),
            active_skills_json=json.dumps(active_skills or [], ensure_ascii=False),
            created_at=_utc_now(),
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO session_checkpoints
                   (session_id, generation, turn_number, file_snapshot_json,
                    pending_tool_ids_json, tool_results_json, active_skills_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (cp.session_id, cp.generation, cp.turn_number,
                 cp.file_snapshot_json, cp.pending_tool_ids_json,
                 cp.tool_results_json, cp.active_skills_json, cp.created_at),
            )
        self.prune(session_id)
        return cp

    # ── restore ─────────────────────────────────────────────────────────

    def restore(self, session_id: str) -> StepCheckpoint | None:
        """Return the most recent checkpoint for *session_id*."""
        self.ensure_table()
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM session_checkpoints
                   WHERE session_id = ?
                   ORDER BY turn_number DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return StepCheckpoint(
            session_id=row["session_id"],
            generation=row["generation"],
            turn_number=row["turn_number"],
            file_snapshot_json=row["file_snapshot_json"],
            pending_tool_ids_json=row["pending_tool_ids_json"],
            tool_results_json=row["tool_results_json"],
            active_skills_json=row["active_skills_json"],
            created_at=row["created_at"],
        )

    def get_tool_results(self, session_id: str) -> dict[str, Any]:
        """Return all completed tool results from the last checkpoint."""
        cp = self.restore(session_id)
        if cp is None:
            return {}
        try:
            return json.loads(cp.tool_results_json)
        except (json.JSONDecodeError, TypeError):
            return {}

    def get_pending_ids(self, session_id: str) -> list[str]:
        """Return pending tool IDs from the last checkpoint."""
        cp = self.restore(session_id)
        if cp is None:
            return []
        try:
            return json.loads(cp.pending_tool_ids_json)
        except (json.JSONDecodeError, TypeError):
            return []

    # ── prune ───────────────────────────────────────────────────────────

    def prune(self, session_id: str, keep_last_n: int | None = None) -> int:
        keep = keep_last_n if keep_last_n is not None else self.KEEP_LAST_N
        with self._connect() as conn:
            cursor = conn.execute(
                """DELETE FROM session_checkpoints
                   WHERE id NOT IN (
                       SELECT id FROM session_checkpoints
                       WHERE session_id = ?
                       ORDER BY turn_number DESC LIMIT ?
                   ) AND session_id = ?""",
                (session_id, keep, session_id),
            )
            return cursor.rowcount

    # ── internal ────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn


# ── IdempotentToolCache ─────────────────────────────────────────────────────

class IdempotentToolCache:
    """Prevents duplicate tool execution during recovery replay.

    After crash recovery, the agent re-executes the turn's tool calls.
    If a tool already completed (result in checkpoint), return the cached
    result instead of re-executing.
    """

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}

    def load_from_checkpoint(self, checkpoint: StepCheckpoint | None) -> None:
        if checkpoint is None:
            return
        try:
            self._cache.update(json.loads(checkpoint.tool_results_json))
        except (json.JSONDecodeError, TypeError):
            pass

    def get(self, invocation_id: str) -> Any | None:
        return self._cache.get(invocation_id)

    def put(self, invocation_id: str, result: Any) -> None:
        self._cache[invocation_id] = result

    def clear(self) -> None:
        self._cache.clear()


# ── helpers ─────────────────────────────────────────────────────────────────

def _serialize_result(result: Any) -> dict:
    """Serialize a ToolResult to a JSON-safe dict for checkpoint storage."""
    if result is None:
        return {"success": True, "output": ""}
    if isinstance(result, dict):
        # Already a JSON-safe dict (e.g. direct tool output) — keep as-is.
        return result
    if hasattr(result, "to_dict"):
        return result.to_dict()
    return {
        "success": getattr(result, "success", True),
        "output": str(getattr(result, "output", "")),
        "error": str(getattr(result, "error", "")) if getattr(result, "error", None) else "",
    }


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
