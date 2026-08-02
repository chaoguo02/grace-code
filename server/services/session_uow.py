"""
R3.2: Session Unit of Work — transaction boundary for state + outbox.

Pattern: Business state writes and outbox INSERT happen in the same
SQLite transaction.  The UoW provides the connection to both.

First-round scope: provide the infrastructure, demonstrate the pattern,
but don't replace existing code paths yet.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Callable

from server.services.event_outbox import OutboxStore

logger = logging.getLogger(__name__)


class SessionUnitOfWork:
    """Transaction boundary for state changes + outbox events.

    Usage:
        uow = SessionUnitOfWork(db_path)
        uow.execute(lambda tx: (
            tx.store.append_event(tx.conn, ...),
            # other state writes on tx.conn
        ))
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._outbox = OutboxStore(db_path)
        self._outbox.install()

    @property
    def outbox(self) -> OutboxStore:
        return self._outbox

    def execute(self, fn: Callable[[sqlite3.Connection], object]) -> None:
        """Run *fn* inside a transaction. Commits on success, rolls back on error."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            fn(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute_with_outbox(
        self,
        fn: Callable[[sqlite3.Connection, OutboxStore], None],
    ) -> None:
        """Run *fn* with both connection and outbox store in same transaction."""
        self.execute(lambda conn: fn(conn, self._outbox))
