"""
P7: Unit of Work contract — state mutation + fact append in same transaction.

Protocol only.  No SQLite, no server imports.  Eliminates agent→server dependency.
"""

from __future__ import annotations

from typing import Protocol, Callable


class TransactionError(RuntimeError):
    """Transaction could not be committed."""


class SessionTransaction(Protocol):
    """Write-side of one transactional boundary.

    Implementations guarantee: state writes + outbox writes
    commit atomically or roll back together.
    """

    def append_fact(self, envelope) -> None:
        """Append a durable fact event to the outbox.

        *envelope* must be an EventEnvelope with a registered payload type.
        This is NOT a dict passthrough.
        """
        ...

    def commit(self) -> None:
        """Commit the transaction.  Raises TransactionError on failure."""
        ...

    def rollback(self) -> None:
        """Roll back the transaction.  Idempotent."""
        ...


class SessionUnitOfWork(Protocol):
    """Transaction boundary for one session-scoped operation.

    Usage:
        uow.execute(lambda tx: (
            tx.append_fact(envelope),
            # ... other state writes via tx methods
        ))
    """

    def execute(self, fn: Callable[[SessionTransaction], None]) -> None:
        """Run *fn* in a transaction.  Commit on success, rollback on error.

        Raises:
            TransactionError: if commit fails.
        """
        ...
