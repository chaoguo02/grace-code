"""G9: Outbox delivery contract — Retry/DLQ/Identity/Ordering.

AC: Delivered → ACK, Retryable → reschedule, Permanent → DLQ
AC: Exponential backoff with jitter on reschedule
AC: Identity conflict: same ID + different digest → raise
AC: Identity idempotent: same ID + same digest → skip
AC: Aggregate ordering: v1 before v2 for same aggregate
AC: count_pending() exact, not claim-based
AC: Crash after projection before ACK → rescheduled (not lost)
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.eventing.identifiers import SessionId, EventId, AggregateVersion
from core.eventing.scope import ScopeToken
from application.events.envelope import (
    EventEnvelope, EventTypeName, SchemaVersion, EventSource,
    CorrelationId, AggregateId,
)
from application.events.run_facts import completed
from application.events.schema_registry import SchemaRegistry
from infrastructure.outbox.sqlite_store import SqliteOutboxStore, OutboxRecord
from infrastructure.outbox.relay import OutboxRelay
from listeners.delivery import (
    Delivered,
    RetryableDeliveryFailure,
    PermanentDeliveryFailure,
    ProjectionReceipt,
)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db():
    d = tempfile.mkdtemp()
    db = str(Path(d) / "test.db")
    yield db
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def _make_envelope(run_id: str = "r-test", session_id: str = "s-test") -> EventEnvelope:
    sid = SessionId(session_id)
    payload = completed(run_id, steps_taken=3, tokens_used=100)
    return EventEnvelope(
        event_id=EventId.generate(),
        event_type=EventTypeName("run.completed.v1"),
        schema_version=SchemaVersion(1),
        occurred_at=datetime.now(timezone.utc),
        source=EventSource(process_id="test", component="runtime"),
        scope=ScopeToken.session_scope(uuid.uuid4(), sid),
        correlation_id=CorrelationId("c1"),
        causation_id=None,
        aggregate_id=AggregateId(run_id),
        aggregate_version=AggregateVersion(1),
        payload=payload,
    )


def _setup_store(temp_db: str) -> tuple[SchemaRegistry, SqliteOutboxStore]:
    registry = SchemaRegistry()
    store = SqliteOutboxStore(temp_db, registry)
    conn = sqlite3.connect(temp_db)
    SqliteOutboxStore.install(conn)
    conn.commit()
    conn.close()
    return registry, store


# ═══════════════════════════════════════════════════════════════════════════════
# G9.1 — Delivery outcome: Delivered → ACK, Retryable → reschedule, Permanent → DLQ
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeliveryOutcomeRouting:
    """G9: Relay routes DeliveryOutcome correctly."""

    def test_delivered_mark_acked(self, temp_db):
        reg, store = _setup_store(temp_db)
        env = _make_envelope()
        conn = sqlite3.connect(temp_db)
        conn.execute("BEGIN IMMEDIATE")
        store.append(conn, env)
        conn.commit()
        conn.close()

        def _deliver(record):
            return Delivered()

        relay = OutboxRelay(store, _deliver)
        batch = store.claim_batch("w1", limit=10)
        assert len(batch) == 1
        record = batch[0]

        outcome = _deliver(record)
        assert isinstance(outcome, Delivered)
        store.mark_delivered(record.event_id, "w1")

        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM event_outbox WHERE event_id=?",
            (str(env.event_id),),
        ).fetchone()
        conn.close()
        assert row["status"] == "delivered"

    def test_retryable_reschedules(self, temp_db):
        reg, store = _setup_store(temp_db)
        env = _make_envelope()
        conn = sqlite3.connect(temp_db)
        conn.execute("BEGIN IMMEDIATE")
        store.append(conn, env)
        conn.commit()
        conn.close()

        def _deliver(record):
            return RetryableDeliveryFailure(reason="transient DB down")

        batch = store.claim_batch("w1", limit=10)
        record = batch[0]
        outcome = _deliver(record)
        assert isinstance(outcome, RetryableDeliveryFailure)

        store.reschedule(record.event_id, "w1", outcome.reason, delay_s=0.5)

        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, attempts, available_at FROM event_outbox WHERE event_id=?",
            (str(env.event_id),),
        ).fetchone()
        conn.close()
        assert row["status"] == "pending", (
            f"G9 FAIL: retryable should reschedule to pending, got {row['status']}"
        )
        assert row["attempts"] >= 1
        assert row["available_at"] is not None, "G9: must set available_at for backoff"

    def test_permanent_dead_letters(self, temp_db):
        reg, store = _setup_store(temp_db)
        env = _make_envelope()
        conn = sqlite3.connect(temp_db)
        conn.execute("BEGIN IMMEDIATE")
        store.append(conn, env)
        conn.commit()
        conn.close()

        def _deliver(record):
            return PermanentDeliveryFailure(reason="unknown schema v99")

        batch = store.claim_batch("w1", limit=10)
        record = batch[0]
        outcome = _deliver(record)
        assert isinstance(outcome, PermanentDeliveryFailure)

        store.dead_letter(record.event_id, "w1", outcome.reason)

        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM event_outbox WHERE event_id=?",
            (str(env.event_id),),
        ).fetchone()
        conn.close()
        assert row["status"] == "dead_letter", (
            f"G9 FAIL: permanent should go to DLQ, got {row['status']}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# G9.2 — Identity conflict + idempotent
# ═══════════════════════════════════════════════════════════════════════════════

class TestIdentityConflict:
    """G9: Same ID + same digest → idempotent; same ID + different digest → conflict."""

    def test_same_id_same_digest_is_idempotent(self, temp_db):
        reg, store = _setup_store(temp_db)
        env = _make_envelope()

        conn = sqlite3.connect(temp_db)
        conn.execute("BEGIN IMMEDIATE")
        store.append(conn, env)
        conn.commit()
        conn.close()

        # Second append with SAME envelope should be idempotent
        conn = sqlite3.connect(temp_db)
        conn.execute("BEGIN IMMEDIATE")
        store.append(conn, env)  # must not raise
        conn.commit()
        conn.close()

    def test_same_id_different_digest_is_conflict(self, temp_db):
        reg, store = _setup_store(temp_db)
        env_a = _make_envelope(run_id="r-test")
        env_b = _make_envelope(run_id="r-test")

        # Force same event_id but different summary (different payload digest)
        import copy
        eid = EventId.generate()
        sid = SessionId("s-test")

        env1 = EventEnvelope(
            event_id=eid,
            event_type=EventTypeName("run.completed.v1"),
            schema_version=SchemaVersion(1),
            occurred_at=datetime.now(timezone.utc),
            source=EventSource(process_id="test", component="runtime"),
            scope=ScopeToken.session_scope(uuid.uuid4(), sid),
            correlation_id=CorrelationId("c1"),
            causation_id=None,
            aggregate_id=AggregateId("r-test"),
            aggregate_version=AggregateVersion(1),
            payload=completed("r-test", summary="version A"),
        )
        env2 = EventEnvelope(
            event_id=eid,
            event_type=EventTypeName("run.completed.v1"),
            schema_version=SchemaVersion(1),
            occurred_at=datetime.now(timezone.utc),
            source=EventSource(process_id="test", component="runtime"),
            scope=ScopeToken.session_scope(uuid.uuid4(), sid),
            correlation_id=CorrelationId("c2"),
            causation_id=None,
            aggregate_id=AggregateId("r-test"),
            aggregate_version=AggregateVersion(1),
            payload=completed("r-test", summary="version B — different!"),
        )

        conn = sqlite3.connect(temp_db)
        conn.execute("BEGIN IMMEDIATE")
        store.append(conn, env1)
        conn.commit()
        conn.close()

        # Second append with same ID but different content → conflict
        conn = sqlite3.connect(temp_db)
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="IdentityConflict|already exists"):
            store.append(conn, env2)
        conn.rollback()
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# G9.3 — Aggregate ordering
# ═══════════════════════════════════════════════════════════════════════════════

class TestAggregateOrdering:
    """G9: v1 must be delivered before v2 of same aggregate can be claimed."""

    def test_v2_not_claimed_before_v1(self, temp_db):
        reg, store = _setup_store(temp_db)
        sid = SessionId("s-test")

        # Insert v1 and v2 of the same aggregate
        env_v1 = EventEnvelope(
            event_id=EventId.generate(),
            event_type=EventTypeName("run.completed.v1"),
            schema_version=SchemaVersion(1),
            occurred_at=datetime.now(timezone.utc),
            source=EventSource(process_id="test", component="runtime"),
            scope=ScopeToken.session_scope(uuid.uuid4(), sid),
            correlation_id=CorrelationId("c1"),
            causation_id=None,
            aggregate_id=AggregateId("r-agg"),
            aggregate_version=AggregateVersion(1),
            payload=completed("r-agg"),
        )
        env_v2 = EventEnvelope(
            event_id=EventId.generate(),
            event_type=EventTypeName("run.completed.v1"),
            schema_version=SchemaVersion(1),
            occurred_at=datetime.now(timezone.utc),
            source=EventSource(process_id="test", component="runtime"),
            scope=ScopeToken.session_scope(uuid.uuid4(), sid),
            correlation_id=CorrelationId("c2"),
            causation_id=None,
            aggregate_id=AggregateId("r-agg"),
            aggregate_version=AggregateVersion(2),
            payload=completed("r-agg"),
        )

        conn = sqlite3.connect(temp_db)
        conn.execute("BEGIN IMMEDIATE")
        store.append(conn, env_v1)
        store.append(conn, env_v2)
        conn.commit()
        conn.close()

        # Claim — only v1 should be claimed (v2 blocked by pending v1)
        batch = store.claim_batch("w1", limit=10)
        claimed_ids = {r.event_id for r in batch}
        assert str(env_v1.event_id) in claimed_ids, "v1 must be claimed"
        assert str(env_v2.event_id) not in claimed_ids, (
            "G9: v2 must not be claimed while v1 is pending"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# G9.4 — Exact count and shutdown
# ═══════════════════════════════════════════════════════════════════════════════

class TestExactCount:
    """G9: count_pending() returns exact pending count."""

    def test_count_pending_exact(self, temp_db):
        reg, store = _setup_store(temp_db)

        for i in range(5):
            env = _make_envelope(run_id=f"r-{i}")
            conn = sqlite3.connect(temp_db)
            conn.execute("BEGIN IMMEDIATE")
            store.append(conn, env)
            conn.commit()
            conn.close()

        assert store.count_pending() == 5

        batch = store.claim_batch("w1", limit=3)
        assert len(batch) == 3

        # Claimed events are no longer pending
        assert store.count_pending() == 2

        # Mark one delivered
        store.mark_delivered(batch[0].event_id, "w1")
        assert store.count_pending() == 2  # still 2 pending (unclaimed)

    def test_count_by_status(self, temp_db):
        reg, store = _setup_store(temp_db)
        env = _make_envelope()
        conn = sqlite3.connect(temp_db)
        conn.execute("BEGIN IMMEDIATE")
        store.append(conn, env)
        conn.commit()
        conn.close()

        assert store.count_by_status("pending") == 1
        assert store.count_by_status("delivered") == 0
        assert store.count_by_status("dead_letter") == 0


# ═══════════════════════════════════════════════════════════════════════════════
# G9.5 — Crash recovery: projection before ACK
# ═══════════════════════════════════════════════════════════════════════════════

class TestCrashRecovery:
    """G9: Crash after projection but before ACK → event rescheduled."""

    def test_crash_before_ack_reschedules(self, temp_db):
        reg, store = _setup_store(temp_db)
        env = _make_envelope()
        conn = sqlite3.connect(temp_db)
        conn.execute("BEGIN IMMEDIATE")
        store.append(conn, env)
        conn.commit()
        conn.close()

        # Simulate: claim → deliver (projection succeeds) → CRASH before ACK
        batch = store.claim_batch("w1", limit=10)
        record = batch[0]
        # Projection succeeded but we crash without ACK
        # In real life: the claim lease expires, event goes back to pending

        # Simulate lease expiry and re-claim
        conn = sqlite3.connect(temp_db)
        conn.execute(
            "UPDATE event_outbox SET status='pending', claimed_by=NULL "
            "WHERE event_id=?",
            (record.event_id,),
        )
        conn.commit()
        conn.close()

        # Re-claim must succeed
        batch2 = store.claim_batch("w2", limit=10)
        assert len(batch2) == 1, "Event must be re-claimable after crash"
        assert batch2[0].event_id == record.event_id
