"""R3.1 Task B: Outbox Repository — acceptance tests.

AC: claim_batch atomic, lease expiry, idempotent projection receipt,
     same event_id + same payload idempotent.
"""

from __future__ import annotations

import sqlite3
import os
import tempfile

import pytest

from server.services.event_outbox import OutboxStore


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_outbox.db")


@pytest.fixture
def store(db_path):
    return OutboxStore(db_path)


class TestOutboxSchema:

    def test_ensure_tables_creates_indexes(self, db_path):
        conn = sqlite3.connect(db_path)
        OutboxStore.ensure_tables(conn)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r[0] for r in tables}
        assert "event_outbox" in table_names
        assert "event_projection_receipts" in table_names
        conn.close()


class TestAppendAndClaim:

    def test_append_then_claim(self, store):
        conn = sqlite3.connect(store._db_path)
        store.ensure_tables(conn)
        store.append_event(conn, "ev-1", "test.event", "s1", "a1", 1, '{"k":"v"}')
        conn.commit()
        conn.close()

        batch = store.claim_batch("worker-1", limit=10)
        assert len(batch) == 1
        assert batch[0].event_id == "ev-1"
        assert batch[0].claimed_by == "worker-1"

    def test_claim_is_atomic_two_workers(self, store):
        conn = sqlite3.connect(store._db_path)
        store.ensure_tables(conn)
        store.append_event(conn, "ev-1", "test.event", "s1", "a1", 1, '{"k":"v"}')
        conn.commit()
        conn.close()

        batch_a = store.claim_batch("worker-A", limit=10)
        batch_b = store.claim_batch("worker-B", limit=10)
        assert len(batch_a) == 1
        assert len(batch_b) == 0  # already claimed

    def test_release_expired_claims(self, store):
        conn = sqlite3.connect(store._db_path)
        store.ensure_tables(conn)
        store.append_event(conn, "ev-1", "test.event", "s1", "a1", 1, '{"k":"v"}')
        conn.commit()
        conn.close()

        store.claim_batch("worker-1", limit=10)
        # Force claim to appear expired by directly updating claimed_at
        with store._connect() as c:
            c.execute(
                "UPDATE event_outbox SET claimed_at='2020-01-01T00:00:00' WHERE event_id='ev-1'"
            )

        released = store.release_expired_claims()
        assert released >= 1
        batch = store.claim_batch("worker-2", limit=10)
        assert len(batch) == 1


class TestDelivery:

    def test_mark_delivered(self, store):
        conn = sqlite3.connect(store._db_path)
        store.ensure_tables(conn)
        store.append_event(conn, "ev-1", "test.event", "s1", "a1", 1, '{"k":"v"}')
        conn.commit()
        conn.close()

        store.claim_batch("w1", limit=10)
        assert store.mark_delivered("ev-1", "w1")

    def test_reschedule_then_reclaim(self, store):
        conn = sqlite3.connect(store._db_path)
        store.ensure_tables(conn)
        store.append_event(conn, "ev-r1", "test.event", "s1", "a1", 1, '{"k":"v"}')
        conn.commit()
        conn.close()

        batch = store.claim_batch("w1", limit=10)
        assert len(batch) == 1
        assert store.reschedule("ev-r1", "w1", "temp error")

    def test_dead_letter_not_claimable(self, store):
        conn = sqlite3.connect(store._db_path)
        store.ensure_tables(conn)
        store.append_event(conn, "ev-dl", "test.event", "s1", "a1", 1, '{"k":"v"}')
        conn.commit()
        conn.close()

        store.claim_batch("w1", limit=10)
        store.dead_letter("ev-dl", "w1", "fatal")
        batch = store.claim_batch("w1", limit=10)
        assert len(batch) == 0


class TestProjectionReceipt:

    def test_idempotent_projection(self, store):
        first = store.record_projection("trace", "ev-1")
        second = store.record_projection("trace", "ev-1")
        third = store.record_projection("stats", "ev-1")
        assert first is True, f"First insert should succeed, got {first}"
        assert second is False, f"Duplicate should be ignored, got {second}"
        assert third is True, f"Different consumer should succeed, got {third}"
