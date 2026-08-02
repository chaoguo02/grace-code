"""N0: Outbox delivery failure tests.

AC: decode failure → reschedule, NOT mark_delivered.
AC: projection failure → reschedule/DLQ.
AC: poison message → dead_letter after max attempts.
AC: good events delivered even when bad events exist in same batch.
"""

from __future__ import annotations

import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.eventing.identifiers import (
    SessionId, EventId, AggregateVersion,
)
from core.eventing.scope import ScopeToken

from application.events.envelope import (
    EventEnvelope, EventTypeName, SchemaVersion, EventSource,
    CorrelationId, AggregateId,
)
from application.events.run_facts import completed
from application.events.schema_registry import SchemaRegistry
from infrastructure.outbox.sqlite_store import SqliteOutboxStore
from infrastructure.outbox.relay import OutboxRelay
from eventing.scoped_bus import ScopedEventBus


@pytest.fixture
def temp_db():
    d = tempfile.mkdtemp()
    db = str(Path(d) / "test.db")
    yield db
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def _make_envelope(session_id="s-test"):
    sid = SessionId(session_id)
    return EventEnvelope(
        event_id=EventId.generate(),
        event_type=EventTypeName("run.completed.v1"),
        schema_version=SchemaVersion(1),
        occurred_at=datetime.now(timezone.utc),
        source=EventSource(process_id="test", component="runtime"),
        scope=ScopeToken.session_scope(uuid.uuid4(), sid),
        correlation_id=CorrelationId("corr-1"),
        causation_id=None,
        aggregate_id=AggregateId("r-test"),
        aggregate_version=AggregateVersion(1),
        payload=completed("r-test", steps_taken=3, tokens_used=100),
    )


class TestDeliveryFailure:

    def test_decode_failure_reschedules_not_delivers(self, temp_db):
        """Invalid JSON in outbox → delivery throws → event rescheduled."""
        registry = SchemaRegistry()
        outbox = SqliteOutboxStore(temp_db, registry)
        bus = ScopedEventBus()

        # Install tables
        conn = sqlite3.connect(temp_db)
        SqliteOutboxStore.install(conn)
        conn.commit()

        # Insert a bad record directly (not through append, to bypass validation)
        bad_event_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO event_outbox
               (event_id, event_type, session_id, aggregate_id,
                aggregate_version, payload_json, occurred_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (bad_event_id, "run.completed.v1", "s-test", "r-test",
             1, '{"not": "valid envelope json', datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()

        # Delivery that throws on decode
        delivered_events = []

        def _deliver(record):
            if record.event_id == bad_event_id:
                # Simulate what happens: decode fails
                registry.decode(record.payload_json)
            envelope = registry.decode(record.payload_json)
            bus.publish(envelope)
            delivered_events.append(record.event_id)

        relay = OutboxRelay(outbox, _deliver)

        # Manually simulate one poll cycle
        batch = outbox.claim_batch("test-worker", limit=10)
        assert len(batch) >= 1

        for record in batch:
            try:
                _deliver(record)
                outbox.mark_delivered(record.event_id, "test-worker")
            except Exception as exc:
                new_attempts = record.attempts + 1
                if new_attempts >= OutboxRelay.MAX_ATTEMPTS:
                    outbox.dead_letter(record.event_id, "test-worker", str(exc)[:500])
                else:
                    outbox.reschedule(record.event_id, "test-worker", str(exc)[:500])

        # Verify: bad event was NOT delivered
        assert bad_event_id not in delivered_events

        # Verify: bad event was rescheduled (status=pending, attempts incremented)
        conn2 = sqlite3.connect(temp_db)
        conn2.row_factory = sqlite3.Row
        row = conn2.execute(
            "SELECT status, attempts FROM event_outbox WHERE event_id=?",
            (bad_event_id,),
        ).fetchone()
        conn2.close()
        assert row is not None
        assert row["status"] == "pending", (
            f"N0 FAIL: event should be rescheduled (pending), got {row['status']}"
        )
        assert row["attempts"] >= 1, (
            f"N0 FAIL: attempts should be >=1, got {row['attempts']}"
        )

    def test_poison_message_reaches_dlq_after_max_attempts(self, temp_db):
        """Event that always fails → dead_letter after MAX_ATTEMPTS."""
        registry = SchemaRegistry()
        outbox = SqliteOutboxStore(temp_db, registry)

        conn = sqlite3.connect(temp_db)
        SqliteOutboxStore.install(conn)
        conn.commit()
        conn.close()

        # Insert a valid envelope
        env = _make_envelope()
        conn = sqlite3.connect(temp_db)
        conn.execute("BEGIN IMMEDIATE")
        outbox.append(conn, env)
        conn.commit()
        conn.close()

        # Delivery that always fails
        def _failing_deliver(record):
            raise RuntimeError("projection DB is down")

        # Simulate MAX_ATTEMPTS retries
        event_id = str(env.event_id)
        for attempt in range(OutboxRelay.MAX_ATTEMPTS):
            batch = outbox.claim_batch("test-worker", limit=10)
            matching = [r for r in batch if r.event_id == event_id]
            if not matching:
                # Already dead-lettered or status changed
                break
            record = matching[0]
            try:
                _failing_deliver(record)
                outbox.mark_delivered(record.event_id, "test-worker")
            except Exception as exc:
                new_attempts = record.attempts + 1
                if new_attempts >= OutboxRelay.MAX_ATTEMPTS:
                    outbox.dead_letter(record.event_id, "test-worker", str(exc)[:500])
                else:
                    outbox.reschedule(record.event_id, "test-worker", str(exc)[:500])

        # Verify: poison message reached DLQ
        conn2 = sqlite3.connect(temp_db)
        conn2.row_factory = sqlite3.Row
        row = conn2.execute(
            "SELECT status, attempts FROM event_outbox WHERE event_id=?",
            (event_id,),
        ).fetchone()
        conn2.close()
        assert row is not None
        assert row["status"] == "dead_letter", (
            f"N0 FAIL: poison should be dead_letter after {OutboxRelay.MAX_ATTEMPTS} "
            f"attempts, got {row['status']}"
        )
        assert row["attempts"] >= OutboxRelay.MAX_ATTEMPTS

    def test_good_and_bad_events_in_same_batch(self, temp_db):
        """Good event delivered; bad event rescheduled — not both lost."""
        registry = SchemaRegistry()
        outbox = SqliteOutboxStore(temp_db, registry)
        bus = ScopedEventBus()
        bus.ensure_session(SessionId("s-test"))

        conn = sqlite3.connect(temp_db)
        SqliteOutboxStore.install(conn)
        conn.commit()

        # Good envelope
        good_env = _make_envelope("s-test")
        conn.execute("BEGIN IMMEDIATE")
        outbox.append(conn, good_env)
        conn.commit()

        # Bad record
        bad_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO event_outbox
               (event_id, event_type, session_id, aggregate_id,
                aggregate_version, payload_json, occurred_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (bad_id, "run.completed.v1", "s-test", "r-test",
             1, '{{broken', datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()

        delivered = []
        failed = []

        def _deliver(record):
            envelope = registry.decode(record.payload_json)
            bus.publish(envelope)
            delivered.append(record.event_id)

        batch = outbox.claim_batch("test-worker", limit=10)
        assert len(batch) >= 2

        for record in batch:
            try:
                _deliver(record)
                outbox.mark_delivered(record.event_id, "test-worker")
            except Exception as exc:
                failed.append(record.event_id)
                outbox.reschedule(record.event_id, "test-worker", str(exc)[:500])

        good_id = str(good_env.event_id)
        assert good_id in delivered, "Good event must be delivered"
        assert bad_id in failed, "Bad event must fail"
        assert good_id not in failed, "Good event must NOT be in failed list"

        # Verify DB status
        conn2 = sqlite3.connect(temp_db)
        conn2.row_factory = sqlite3.Row
        good_row = conn2.execute(
            "SELECT status FROM event_outbox WHERE event_id=?", (good_id,)
        ).fetchone()
        bad_row = conn2.execute(
            "SELECT status FROM event_outbox WHERE event_id=?", (bad_id,)
        ).fetchone()
        conn2.close()

        assert good_row["status"] == "delivered"
        assert bad_row["status"] == "pending"  # rescheduled
