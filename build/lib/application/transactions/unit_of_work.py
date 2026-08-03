"""
P7: Unit of Work contract — state mutation + fact append in same transaction.

Protocol only.  No SQLite, no server imports.  Eliminates agent→server dependency.
"""

from __future__ import annotations

from typing import Protocol, Callable, TypeVar

T = TypeVar("T")


class TransactionError(RuntimeError):
    """Transaction could not be committed."""


class SessionTransaction(Protocol):
    """Write-side of one transactional boundary.

    Implementations guarantee: state writes + outbox writes
    commit atomically or roll back together.
    """

    # ── State mutation methods ───────────────────────────────────────────

    def increment_generation(self, session_id) -> int:
        """Increment session's run_generation, return the NEW turn_index.

        Must be called inside a transaction.  Raises ValueError if session
        does not exist.
        """
        ...

    def create_run(self, *, run_id, session_id, turn_id, turn_index,
                   idempotency_key: str, prompt: str) -> None:
        """Insert a row into the runs table with status='queued'."""
        ...

    def insert_message(self, *, session_id, role: str, content: str,
                       turn_id: str) -> None:
        """Insert a row into session_messages."""
        ...

    def append_fact(self, envelope) -> None:
        """Append a durable fact event to the outbox.

        *envelope* must be an EventEnvelope with a registered payload type.
        This is NOT a dict passthrough.
        """
        ...

    # ── Transaction control ──────────────────────────────────────────────

    def commit(self) -> None:
        """Commit the transaction.  Raises TransactionError on failure."""
        ...

    def rollback(self) -> None:
        """Roll back the transaction.  Idempotent."""
        ...


class SessionUnitOfWork(Protocol):
    """Transaction boundary for one session-scoped operation.

    Usage:
        result = uow.execute(lambda tx: (
            tx.increment_generation(sid),
            tx.create_run(...),
            tx.insert_message(...),
            tx.append_fact(envelope),
        ))
    """

    def execute(self, fn: Callable[[SessionTransaction], T]) -> T:
        """Run *fn* in a transaction.  Commit on success, rollback on error.

        Returns whatever *fn* returns.

        Raises:
            TransactionError: if commit fails.
        """
        ...
