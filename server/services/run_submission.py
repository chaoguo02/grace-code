"""Atomic Run/Turn submission shared by chat and Plan approval actions."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from server.domain_events import DomainEvent
from server.services.event_outbox import OutboxStore


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
    """Create run, turn index, and user message in one SQLite transaction.

    P19: GRACE_RUNTIME_MODE=NATIVE routes to RunCoordinator.
    LEGACY (default) uses the inline SQLite path.
    """
    import os as _os
    if _os.environ.get("GRACE_RUNTIME_MODE") == "NATIVE":
        return _submit_via_coordinator(
            storage, session_id=session_id, prompt=prompt,
            idempotency_key=idempotency_key,
        )

    key = idempotency_key.strip()
    run_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    outbox = OutboxStore(storage._db_path)
    outbox.install()

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
            outbox.append(conn, DomainEvent(
                event_type="run.submitted",
                session_id=session_id,
                aggregate_id=run_id,
                aggregate_version=1,
                occurred_at=now,
                payload={
                    "turn_id": turn_id,
                    "turn_index": turn_index,
                    "idempotency_key": key,
                },
            ))
            conn.execute("COMMIT")
            return SubmittedRun(
                run_id=run_id,
                turn_id=turn_id,
                turn_index=turn_index,
                created=True,
            )
    except sqlite3.IntegrityError as exc:
        raise RunAlreadyActiveError("RUN_ALREADY_ACTIVE") from exc


def _submit_via_coordinator(storage, *, session_id: str, prompt: str,
                            idempotency_key: str = "") -> SubmittedRun:
    """P19: Route run submission through the new RunCoordinator.

    Pre-checks (idempotency, active-run) execute on a read connection.
    The happy-path transaction runs through RunCoordinator.submit() which
    orchestrates: increment_generation → create_run → insert_message →
    append_fact — all in one SQLite transaction via SqliteOutboxStore.
    """
    from core.eventing.identifiers import SessionId
    from application.commands.run_commands import SubmitRun
    from application.coordinators.run_coordinator import RunCoordinator
    from application.events.schema_registry import SchemaRegistry
    from runtime_core.runtime import AgentRuntime
    from runtime_core.ports import RuntimePorts
    from infrastructure.outbox.sqlite_store import SqliteOutboxStore

    key = idempotency_key.strip()
    db_path = storage._db_path

    # ── Pre-checks (read-only, same semantics as LEGACY path) ──────────
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if key:
            existing = conn.execute(
                """SELECT id, turn_id, turn_index, prompt
                   FROM runs WHERE session_id = ? AND idempotency_key = ?""",
                (session_id, key),
            ).fetchone()
            if existing is not None:
                if existing["prompt"] != prompt:
                    raise IdempotencyConflictError(
                        "idempotency key reused with different prompt"
                    )
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
            raise RunAlreadyActiveError("RUN_ALREADY_ACTIVE")
    finally:
        conn.close()

    # ── Happy path: coordinator in real transaction ────────────────────
    registry = SchemaRegistry()

    class _StorageUoW:
        def execute(self, fn):
            conn2 = sqlite3.connect(db_path)
            conn2.row_factory = sqlite3.Row
            outbox = SqliteOutboxStore(db_path, registry)
            try:
                conn2.execute("BEGIN IMMEDIATE")
                result = fn(_StorageTx(conn2, outbox))
                conn2.commit()
                return result
            except Exception:
                conn2.rollback()
                raise
            finally:
                conn2.close()

    class _StorageTx:
        __slots__ = ("conn", "_outbox")

        def __init__(self, conn, outbox_store):
            self.conn = conn
            self._outbox = outbox_store

        @staticmethod
        def _to_str(value) -> str:
            return str(value.value) if hasattr(value, 'value') else str(value)

        def increment_generation(self, session_id) -> int:
            sid = self._to_str(session_id)
            self.conn.execute(
                "UPDATE sessions SET run_generation = run_generation + 1 WHERE id = ?",
                (sid,),
            )
            row = self.conn.execute(
                "SELECT run_generation FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown session: {sid}")
            return int(row["run_generation"])

        def create_run(self, *, run_id, session_id, turn_id, turn_index,
                       idempotency_key: str, prompt: str) -> None:
            now = datetime.now(timezone.utc).isoformat()
            sid = self._to_str(session_id)
            rid = self._to_str(run_id)
            self.conn.execute(
                """INSERT INTO runs
                   (id, session_id, turn_id, turn_index, idempotency_key, prompt,
                    status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                (rid, sid, turn_id, turn_index, idempotency_key, prompt, now, now),
            )

        def insert_message(self, *, session_id, role: str, content: str,
                           turn_id: str) -> None:
            now = datetime.now(timezone.utc).isoformat()
            sid = self._to_str(session_id)
            self.conn.execute(
                """INSERT INTO session_messages
                   (session_id, role, content, turn_id, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (sid, role, content, turn_id, now),
            )

        def append_fact(self, envelope) -> None:
            self._outbox.append(self.conn, envelope)

    uow = _StorageUoW()
    rt = AgentRuntime(RuntimePorts())
    coord = RunCoordinator(rt, uow)

    cmd = SubmitRun(session_id=SessionId(session_id), prompt=prompt,
                    idempotency_key=key)
    envelope = coord.submit(cmd)

    payload = envelope.payload
    return SubmittedRun(
        run_id=str(envelope.aggregate_id),
        turn_id=payload.turn_id,
        turn_index=payload.turn_index,
        created=True,
    )
