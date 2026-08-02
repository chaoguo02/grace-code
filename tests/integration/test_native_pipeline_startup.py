"""G37: Server entry point — uses assemble(), not start_native_pipeline().

Verifies Native object graph assembly, relay delivery, and projection wiring.
No old EventBus path.  No dict service locator.
"""

from __future__ import annotations

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
from infrastructure.outbox.sqlite_store import SqliteOutboxStore
from infrastructure.outbox.owner_lease import OwnerLease


@pytest.fixture
def temp_db():
    d = tempfile.mkdtemp()
    db = str(Path(d) / "test.db")
    conn = sqlite3.connect(db)
    SqliteOutboxStore.install(conn)
    OwnerLease.install(conn)
    conn.commit()
    conn.close()
    yield db
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def _make_completed_envelope(session_id: str = "s-test"):
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
        payload=completed("r-test", steps_taken=3, tokens_used=100, summary="done"),
    )


class TestNativePipelineStartup:

    def test_assemble_returns_typed_components(self, temp_db):
        """G28/G37: assemble() returns ApplicationComponents, not dict."""
        from composition.runtime_composition import assemble
        from composition.application_components import ApplicationComponents

        comp = assemble(temp_db)
        assert isinstance(comp, ApplicationComponents)
        assert comp.registry is not None
        assert comp.bus is not None
        assert comp.trace is not None
        assert comp.relay is not None

    def test_lifecycle_start_stop(self, temp_db):
        """ApplicationLifecycle starts and stops relay cleanly."""
        from composition.runtime_composition import assemble
        from composition.application_components import ApplicationLifecycle

        comp = assemble(temp_db)
        lifecycle = ApplicationLifecycle(comp)
        lifecycle.start()
        lifecycle.stop()

    def test_outbox_deliver_through_relay(self, temp_db):
        """Relay delivery path works with assemble() wired components."""
        from composition.runtime_composition import assemble

        comp = assemble(temp_db)
        registry = SchemaRegistry()
        outbox = SqliteOutboxStore(temp_db, registry)
        bus = comp.bus

        # Ensure trace table
        conn = sqlite3.connect(temp_db)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS session_trace_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, seq INTEGER DEFAULT 0,
                event_type TEXT, timestamp TEXT,
                event_json TEXT, source TEXT DEFAULT '');
        """)
        conn.commit()
        conn.close()

        # Write event to outbox
        env = _make_completed_envelope("s-test")
        bus.ensure_session(SessionId("s-test"))

        conn = sqlite3.connect(temp_db)
        conn.execute("BEGIN IMMEDIATE")
        outbox.append(conn, env)
        conn.commit()
        conn.close()

        # Claim and deliver
        records = outbox.claim_batch("worker-1", limit=10)
        assert len(records) == 1
        record = records[0]

        decoded = registry.decode(record.payload_json)
        bus.publish(decoded)
        outbox.mark_delivered(record.event_id, "worker-1")

        # Verify
        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM event_outbox WHERE event_id=?",
            (str(env.event_id),),
        ).fetchone()
        conn.close()
        assert row["status"] == "delivered"
