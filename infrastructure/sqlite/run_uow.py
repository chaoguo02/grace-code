"""
G21: SQLite UnitOfWork adapter — state mutation + outbox same transaction.

- Typed parameters (no untyped session_id/envelope).
- begin/commit/rollback managed by UoW only.
- Failure injection points for testing atomicity.
- DDL NOT installed in request path (migration-only).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from application.events.envelope import EventEnvelope
from application.transactions.unit_of_work import (
    SessionTransaction, SessionUnitOfWork, TransactionError,
)
from infrastructure.outbox.sqlite_store import SqliteOutboxStore


@dataclass(frozen=True, slots=True)
class RunParams:
    run_id: str
    session_id: str
    turn_id: str
    turn_index: int
    idempotency_key: str
    prompt: str


class SqliteSessionTransaction(SessionTransaction):
    """Concrete SQLite transaction — state + outbox same connection."""

    def __init__(self, conn: sqlite3.Connection, outbox: SqliteOutboxStore,
                 fail_at: str | None = None) -> None:
        self._conn = conn
        self._outbox = outbox
        self._fail_at = fail_at  # G21: failure injection point
        self._committed = False

    def _maybe_fail(self, point: str) -> None:
        if self._fail_at == point:
            raise TransactionError(f"Injected failure at {point}")

    def increment_generation(self, session_id: str) -> int:
        self._maybe_fail("generation")
        row = self._conn.execute(
            "SELECT run_generation FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Session {session_id} not found")
        new_gen = (row[0] if row[0] is not None else 0) + 1
        self._conn.execute(
            "UPDATE sessions SET run_generation=? WHERE id=?",
            (new_gen, session_id),
        )
        return new_gen

    def create_run(self, *, run_id: str, session_id: str, turn_id: str,
                   turn_index: int, idempotency_key: str, prompt: str) -> None:
        self._maybe_fail("run_insert")
        self._conn.execute(
            """INSERT OR IGNORE INTO runs (id, session_id, turn_id, turn_index,
               idempotency_key, prompt, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'queued', datetime('now'))""",
            (run_id, session_id, turn_id, turn_index, idempotency_key, prompt),
        )

    def insert_message(self, *, session_id: str, role: str, content: str,
                       turn_id: str) -> None:
        self._maybe_fail("message_insert")
        self._conn.execute(
            """INSERT INTO session_messages (session_id, role, content,
               turn_id, created_at) VALUES (?, ?, ?, ?, datetime('now'))""",
            (session_id, role, content, turn_id),
        )

    def append_fact(self, envelope: EventEnvelope) -> None:
        self._maybe_fail("outbox_append")
        self._outbox.append(self._conn, envelope)

    def commit(self) -> None:
        self._maybe_fail("commit")
        self._conn.commit()
        self._committed = True

    def check_active_run(self, session_id: str) -> str | None:
        """Return active run_id or None."""
        self._maybe_fail("active_run_check")
        row = self._conn.execute(
            "SELECT id FROM runs WHERE session_id=? AND status NOT IN "
            "('completed','failed','cancelled') ORDER BY turn_index DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return row[0] if row else None

    def check_idempotency(self, session_id: str, key: str,
                          digest: str) -> tuple[str, str] | None:
        """Check if idempotency key exists.  Returns (run_id, digest) or None."""
        row = self._conn.execute(
            "SELECT id, prompt FROM runs WHERE session_id=? AND idempotency_key=?",
            (session_id, key),
        ).fetchone()
        if row is None:
            return None
        import hashlib
        existing_digest = hashlib.sha256(
            f"{session_id}:{row[1]}:{key}".encode()
        ).hexdigest()
        return (row[0], existing_digest)

    def rollback(self) -> None:
        if not self._committed:
            self._conn.rollback()

    @property
    def is_committed(self) -> bool:
        return self._committed


class SqliteUnitOfWork(SessionUnitOfWork):
    """SQLite-backed Unit of Work.  DDL installed at migration time only."""

    def __init__(self, db_path: str, outbox: SqliteOutboxStore,
                 fail_at: str | None = None) -> None:
        self._db_path = db_path
        self._outbox = outbox
        self._fail_at = fail_at

    def execute(self, fn):
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            conn.execute("BEGIN IMMEDIATE")
            tx = SqliteSessionTransaction(conn, self._outbox,
                                          fail_at=self._fail_at)
            result = fn(tx)
            tx.commit()
            return result
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()
