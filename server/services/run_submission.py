"""Atomic Run/Turn submission shared by chat and Plan approval actions."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone


class RunAlreadyActiveError(RuntimeError):
    pass


class IdempotencyConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class SubmittedRun:
    run_id: str
    turn_id: str
    turn_index: int
    created: bool


def submit_run_turn(
    storage,
    *,
    session_id: str,
    prompt: str,
    idempotency_key: str = "",
) -> SubmittedRun:
    """Create run, turn index, and user message in one SQLite transaction."""
    key = idempotency_key.strip()
    run_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    try:
        with storage._store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")

            if key:
                existing = conn.execute(
                    """SELECT id, turn_id, turn_index, prompt
                       FROM runs
                       WHERE session_id = ? AND idempotency_key = ?""",
                    (session_id, key),
                ).fetchone()
                if existing is not None:
                    if existing["prompt"] != prompt:
                        conn.execute("ROLLBACK")
                        raise IdempotencyConflictError(
                            "idempotency key reused with different prompt"
                        )
                    conn.execute("COMMIT")
                    return SubmittedRun(
                        run_id=existing["id"],
                        turn_id=existing["turn_id"],
                        turn_index=int(existing["turn_index"]),
                        created=False,
                    )

            active = conn.execute(
                """SELECT id FROM runs
                   WHERE session_id = ? AND status IN ('queued', 'running')
                   LIMIT 1""",
                (session_id,),
            ).fetchone()
            if active is not None:
                conn.execute("ROLLBACK")
                raise RunAlreadyActiveError("RUN_ALREADY_ACTIVE")

            conn.execute(
                """UPDATE sessions
                   SET run_generation = run_generation + 1
                   WHERE id = ?""",
                (session_id,),
            )
            row = conn.execute(
                "SELECT run_generation FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise ValueError(f"Unknown session: {session_id}")
            turn_index = int(row["run_generation"])

            conn.execute(
                """INSERT INTO runs
                   (id, session_id, turn_id, turn_index, idempotency_key, prompt,
                    status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                (
                    run_id, session_id, turn_id, turn_index, key, prompt,
                    now, now,
                ),
            )
            conn.execute(
                """INSERT INTO session_messages
                   (session_id, role, content, turn_id, created_at)
                   VALUES (?, 'user', ?, ?, ?)""",
                (session_id, prompt, turn_id, now),
            )
            conn.execute("COMMIT")
            return SubmittedRun(
                run_id=run_id,
                turn_id=turn_id,
                turn_index=turn_index,
                created=True,
            )
    except sqlite3.IntegrityError as exc:
        raise RunAlreadyActiveError("RUN_ALREADY_ACTIVE") from exc
