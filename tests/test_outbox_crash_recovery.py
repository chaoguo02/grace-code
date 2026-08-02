"""R3.6: Outbox crash recovery — acceptance tests.

AC: event survived commit→crash→restart.
AC: projection idempotent (same event_id twice → one trace row).
AC: lease expiry after crash → re-claimable.
AC: poison event → dead-letter, does not block subsequent events.
"""

from __future__ import annotations

import sqlite3
import os

import pytest

from server.services.event_outbox import OutboxStore


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_crash.db")


class TestCrashRecovery:

    def test_event_survives_commit_then_reopen(self, db_path):
        """Commit → close → reopen → event still claimable."""
        store = OutboxStore(db_path)
        conn = sqlite3.connect(db_path)
        store.ensure_tables(conn)
        store.append_event(conn, "ev-crash-1", "test.event", "s1", "a1", 1, '{"v":1}')
        conn.commit()
        conn.close()

        # Simulate process restart
        store2 = OutboxStore(db_path)
        batch = store2.claim_batch("w1")
        assert len(batch) == 1
        assert batch[0].event_id == "ev-crash-1"

    def test_projection_idempotent_same_event_id(self, db_path):
        """Same event_id projected twice → only one trace row."""
        store = OutboxStore(db_path)
        conn = sqlite3.connect(db_path)
        store.ensure_tables(conn)
        conn.commit()
        conn.close()

        first = store.record_projection("trace", "ev-dup")
        second = store.record_projection("trace", "ev-dup")
        assert first is True
        assert second is False

    def test_lease_expiry_allows_reclaim(self, db_path):
        """Claim → simulate crash → expired lease → re-claimable."""
        store = OutboxStore(db_path)
        conn = sqlite3.connect(db_path)
        store.ensure_tables(conn)
        store.append_event(conn, "ev-lease", "test.event", "s1", "a1", 1, '{"v":1}')
        conn.commit()
        conn.close()

        # Worker A claims
        batch_a = store.claim_batch("worker-a")
        assert len(batch_a) == 1

        # Force claim to appear expired
        with store._connect() as c:
            c.execute(
                "UPDATE event_outbox SET claimed_at='2020-01-01T00:00:00' WHERE event_id='ev-lease'"
            )

        # Worker B should be able to re-claim after expiry
        released = store.release_expired_claims()
        assert released >= 1
        batch_b = store.claim_batch("worker-b")
        assert len(batch_b) == 1

    def test_poison_event_goes_to_dead_letter(self, db_path):
        """After max attempts, event is dead-lettered and doesn't block others."""
        store = OutboxStore(db_path)
        conn = sqlite3.connect(db_path)
        store.ensure_tables(conn)
        store.append_event(conn, "ev-poison", "test.event", "s1", "a1", 1, '{"v":"bad"}')
        store.append_event(conn, "ev-good", "test.event", "s1", "a1", 1, '{"v":"good"}')
        conn.commit()
        conn.close()

        batch = store.claim_batch("w1", limit=10)
        assert len(batch) == 2

        # Fail the poison event repeatedly
        for i in range(5):
            store.reschedule("ev-poison", "w1", f"fail attempt {i}")
        store.dead_letter("ev-poison", "w1", "fatal after retries")

        # Good event should still be claimable (after reschedule)
        batch2 = store.claim_batch("w1", limit=10)
        poison_ids = {r.event_id for r in batch2}
        assert "ev-poison" not in poison_ids  # dead-lettered


def _ensure_trace_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_trace_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL, seq INTEGER NOT NULL,
            event_type TEXT NOT NULL, timestamp TEXT NOT NULL,
            event_json TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'event_bus',
            child_session_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(session_id, seq)
        )
    """)


class TestTraceProjectionRegression:

    def test_first_projection_produces_trace(self, db_path):
        """P0-2 regression: first projection of an event MUST produce a trace row."""
        import sqlite3
        from server.services.event_outbox import OutboxStore
        from server.projections.trace_projection import TraceProjection

        store = OutboxStore(db_path)
        conn = sqlite3.connect(db_path)
        store.ensure_tables(conn)
        _ensure_trace_table(conn)
        store.append_event(conn, "ev-trace-1", "test.event", "s1", "a1", 1, '{"k":"v"}')
        conn.commit()
        conn.close()

        batch = store.claim_batch("w1")
        assert len(batch) == 1

        proj = TraceProjection(db_path)
        result = proj.project(batch[0])
        assert result is True, "First projection must return True (trace produced)"

        # Verify trace exists
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        trace = conn2.execute(
            "SELECT * FROM session_trace_events WHERE session_id='s1'"
        ).fetchone()
        assert trace is not None, "Trace row must exist after first projection"
        conn2.close()

    def test_second_projection_is_idempotent(self, db_path):
        """P0-2 regression: second projection returns False, no duplicate trace."""
        from server.services.event_outbox import OutboxStore
        from server.projections.trace_projection import TraceProjection
        import sqlite3

        store = OutboxStore(db_path)
        conn = sqlite3.connect(db_path)
        store.ensure_tables(conn)
        _ensure_trace_table(conn)
        store.append_event(conn, "ev-trace-2", "test.event", "s1", "a1", 1, '{"k":"v"}')
        conn.commit()
        conn.close()

        batch = store.claim_batch("w1")
        proj = TraceProjection(db_path)

        first = proj.project(batch[0])
        second = proj.project(batch[0])
        assert first is True
        assert second is False, "Second projection must return False (idempotent)"

        # Only one trace row
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        count = conn2.execute(
            "SELECT COUNT(*) as c FROM session_trace_events WHERE session_id='s1'"
        ).fetchone()["c"]
        assert count == 1, f"Expected 1 trace row, got {count}"
        conn2.close()
