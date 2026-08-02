"""R3.2 Task C: Session UoW — acceptance tests.

AC: state + outbox in same transaction.
AC: failure before outbox → rollback.
AC: failure after outbox, before commit → rollback.
"""

from __future__ import annotations

import pytest

from server.services.session_uow import SessionUnitOfWork
from server.services.event_outbox import OutboxStore


def _fail(msg: str) -> None:
    raise ValueError(msg)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_uow.db")


class TestSessionUow:

    def test_state_and_outbox_in_same_transaction(self, db_path):
        uow = SessionUnitOfWork(db_path)
        uow.execute(lambda conn: (
            conn.execute("CREATE TABLE IF NOT EXISTS test_state (id TEXT PRIMARY KEY, val INT)"),
            conn.execute("INSERT INTO test_state VALUES ('r1', 42)"),
            uow.outbox.append_event(conn, "ev-1", "test.event", "s1", "r1", 1, '{}'),
        ))
        # Both should be committed
        store = OutboxStore(db_path)
        batch = store.claim_batch("w1")
        assert len(batch) == 1
        assert batch[0].event_id == "ev-1"

    def test_failure_before_outbox_rolls_back_state(self, db_path):
        uow = SessionUnitOfWork(db_path)
        uow.execute(lambda conn: (
            conn.execute("CREATE TABLE IF NOT EXISTS test_state (id TEXT PRIMARY KEY, val INT)"),
            conn.execute("INSERT INTO test_state VALUES ('r2', 99)"),
        ))
        try:
            uow.execute(lambda conn: (
                conn.execute("INSERT INTO test_state VALUES ('r3', 1)"),
                _fail("boom"),
            ))
        except ValueError:
            pass
        # r3 should NOT exist (rolled back)
        import sqlite3
        c = sqlite3.connect(db_path)
        r = c.execute("SELECT val FROM test_state WHERE id='r3'").fetchone()
        assert r is None

    def test_failure_after_outbox_rolls_back_both(self, db_path):
        uow = SessionUnitOfWork(db_path)
        try:
            uow.execute(lambda conn: (
                conn.execute("CREATE TABLE IF NOT EXISTS test_state (id TEXT PRIMARY KEY, val INT)"),
                conn.execute("INSERT INTO test_state VALUES ('r4', 1)"),
                uow.outbox.append_event(conn, "ev-r4", "test.event", "s1", "r4", 1, '{}'),
                _fail("post-outbox crash"),
            ))
        except ValueError:
            pass
        # Neither state nor outbox should be present
        store = OutboxStore(db_path)
        batch = store.claim_batch("w1")
        outbox_ids = {r.event_id for r in batch}
        assert "ev-r4" not in outbox_ids

    def test_atomicity_state_and_outbox_both_committed(self, db_path):
        """P0-1 regression: state=1 AND outbox=1 after commit."""
        uow = SessionUnitOfWork(db_path)
        uow.execute(lambda conn: (
            conn.execute("CREATE TABLE IF NOT EXISTS test_state (id TEXT PRIMARY KEY, val INT)"),
            conn.execute("INSERT INTO test_state VALUES ('r5', 1)"),
            uow.outbox.append_event(conn, "ev-r5", "test.event", "s1", "r5", 1, '{}'),
        ))
        # Both must exist
        import sqlite3
        c = sqlite3.connect(db_path)
        state = c.execute("SELECT val FROM test_state WHERE id='r5'").fetchone()
        assert state is not None and state[0] == 1, f"State should be 1, got {state}"
        store = OutboxStore(db_path)
        batch = store.claim_batch("w1")
        assert len(batch) == 1
        assert batch[0].event_id == "ev-r5"

    def test_atomicity_failure_rolls_back_both(self, db_path):
        """P0-1 regression: state=0 AND outbox=0 after failure."""
        uow = SessionUnitOfWork(db_path)
        uow.execute(lambda conn: (
            conn.execute("CREATE TABLE IF NOT EXISTS test_state (id TEXT PRIMARY KEY, val INT)"),
            conn.execute("INSERT INTO test_state VALUES ('setup', 1)"),
        ))
        try:
            uow.execute(lambda conn: (
                conn.execute("INSERT INTO test_state VALUES ('r6', 1)"),
                uow.outbox.append_event(conn, "ev-r6", "test.event", "s1", "r6", 1, '{}'),
                _fail("mid-transaction crash"),
            ))
        except ValueError:
            pass
        import sqlite3
        c = sqlite3.connect(db_path)
        state = c.execute("SELECT val FROM test_state WHERE id='r6'").fetchone()
        assert state is None, f"State should be rolled back, got {state}"
        store = OutboxStore(db_path)
        batch = store.claim_batch("w1")
        outbox_ids = {r.event_id for r in batch}
        assert "ev-r6" not in outbox_ids, "Outbox should be rolled back"
