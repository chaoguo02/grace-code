"""Phase C: Delivery pipeline integration test.

Verifies: SchemaRegistry.decode → ScopedEventBus → Projections.
The full path from outbox JSON to projection receipt.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.eventing.identifiers import SessionId, RunId, TaskId, EventId, AggregateVersion
from core.eventing.scope import ScopeToken

from application.events.envelope import (
    EventEnvelope, EventTypeName, SchemaVersion, EventSource,
    CorrelationId, AggregateId,
)
from application.events.run_facts import (
    RunCompletedV1, completed,
)
from application.events.schema_registry import SchemaRegistry

from eventing.scoped_bus import ScopedEventBus
from listeners.trace_projection import TraceProjection
from listeners.stats_projection import StatsProjection
from listeners.ws_gateway import WsGateway


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def registry():
    return SchemaRegistry()


@pytest.fixture
def bus():
    return ScopedEventBus()


@pytest.fixture
def db_path():
    d = tempfile.mkdtemp()
    yield str(Path(d) / "test.db")
    import shutil
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def trace_projection(db_path):
    tp = TraceProjection(db_path)
    # Install table
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS event_projection_receipts (
            consumer_name TEXT NOT NULL,
            event_id TEXT NOT NULL,
            processed_at TEXT NOT NULL,
            PRIMARY KEY (consumer_name, event_id)
        );
        CREATE TABLE IF NOT EXISTS session_trace_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            seq INTEGER DEFAULT 0,
            event_type TEXT,
            timestamp TEXT,
            event_json TEXT,
            source TEXT DEFAULT ''
        );
    """)
    conn.commit()
    conn.close()
    return tp


@pytest.fixture
def stats_projection():
    return StatsProjection()


@pytest.fixture
def ws_gateway():
    return WsGateway()


# ── Helper ──────────────────────────────────────────────────────────────────

def _make_envelope(event_type: str, payload, session_id: str = "s-test"):
    sid = SessionId(session_id)
    return EventEnvelope(
        event_id=EventId.generate(),
        event_type=EventTypeName(event_type),
        schema_version=SchemaVersion(1),
        occurred_at=datetime.now(timezone.utc),
        source=EventSource(process_id="test", component="runtime"),
        scope=ScopeToken.session_scope(uuid.uuid4(), sid),
        correlation_id=CorrelationId("corr-1"),
        causation_id=None,
        aggregate_id=AggregateId("r-test"),
        aggregate_version=AggregateVersion(1),
        payload=payload,
    )


# ── Tests ───────────────────────────────────────────────────────────────────

class TestDecodeToBusToProjections:

    def test_roundtrip_decode_and_publish_to_trace(
        self, registry, bus, trace_projection
    ):
        """Envelope → JSON → decode → publish → trace projection receives it."""
        env = _make_envelope(
            "run.completed.v1",
            completed("r-test", turn_index=1, steps_taken=3, tokens_used=500),
        )
        # Ensure the session scope exists before publishing
        sid = SessionId("s-test")
        bus.ensure_session(sid)

        # Round-trip through JSON
        js = env.canonical_json()
        decoded = registry.decode(js)

        # Publish to bus
        bus.subscribe("run.completed.v1", trace_projection.on_event, "trace")
        bus.publish(decoded)

        # Trace projection should have written to DB
        import sqlite3
        conn = sqlite3.connect(trace_projection._db_path)
        conn.row_factory = sqlite3.Row
        receipts = conn.execute(
            "SELECT * FROM event_projection_receipts WHERE consumer_name=?",
            ("trace_projection",)
        ).fetchall()
        traces = conn.execute(
            "SELECT * FROM session_trace_events WHERE event_type=?",
            ("run.completed.v1",)
        ).fetchall()
        conn.close()

        assert len(receipts) == 1, "Trace projection should record receipt"
        assert len(traces) == 1, "Trace projection should write trace event"
        assert traces[0]["session_id"] == "s-test"

    def test_decode_and_publish_to_stats(
        self, registry, bus, stats_projection
    ):
        """Stats projection counts events published via the bus."""
        sid = SessionId("s-test")
        bus.ensure_session(sid)

        env = _make_envelope(
            "run.completed.v1",
            completed("r-test", steps_taken=2),
        )
        js = env.canonical_json()
        decoded = registry.decode(js)

        bus.subscribe("run.completed.v1", stats_projection.on_event, "stats")
        assert len(stats_projection.metrics) == 0

        bus.publish(decoded)
        assert len(stats_projection.metrics) == 1
        assert stats_projection.metrics[0]["event_type"] == "run.completed.v1"
        assert stats_projection.metrics[0]["session_id"] == "s-test"

    def test_decode_and_publish_to_ws_gateway(
        self, registry, bus, ws_gateway
    ):
        """WS gateway broadcasts to session subscribers."""
        sid = SessionId("s-test")
        bus.ensure_session(sid)

        received = []
        ws_gateway.subscribe("s-test", lambda m: received.append(m))

        env = _make_envelope(
            "run.completed.v1",
            completed("r-test", summary="all good"),
            session_id="s-test",
        )
        js = env.canonical_json()
        decoded = registry.decode(js)

        bus.subscribe("run.completed.v1", ws_gateway.on_event, "ws")
        bus.publish(decoded)

        assert len(received) == 1
        assert received[0]["event_type"] == "run.completed.v1"
        assert received[0]["aggregate_id"] == "r-test"

    def test_multiple_projections_receive_same_event(
        self, registry, bus, trace_projection, stats_projection, ws_gateway, db_path
    ):
        """All three projections receive the same published event."""
        sid = SessionId("s-test")
        bus.ensure_session(sid)

        ws_received = []
        ws_gateway.subscribe("s-test", lambda m: ws_received.append(m))

        bus.subscribe("run.completed.v1", trace_projection.on_event, "trace")
        bus.subscribe("run.completed.v1", stats_projection.on_event, "stats")
        bus.subscribe("run.completed.v1", ws_gateway.on_event, "ws")

        env = _make_envelope(
            "run.completed.v1",
            completed("r-test", steps_taken=4, tokens_used=900, summary="done"),
        )
        js = env.canonical_json()
        decoded = registry.decode(js)

        bus.publish(decoded)

        # All three received
        assert len(stats_projection.metrics) == 1
        assert len(ws_received) == 1

        import sqlite3
        conn = sqlite3.connect(db_path)
        receipts = conn.execute(
            "SELECT COUNT(*) as c FROM event_projection_receipts"
        ).fetchone()[0]
        conn.close()
        assert receipts >= 1

    def test_scope_isolation_in_bus_with_projections(
        self, registry, bus, stats_projection
    ):
        """Session A event does NOT reach a Session-B-scoped stats subscriber."""
        sid_a = SessionId("s-a")
        sid_b = SessionId("s-b")
        bus.ensure_session(sid_a)
        bus.ensure_session(sid_b)

        stats_a = StatsProjection()
        stats_b = StatsProjection()

        scope_a = bus._tree.ensure_session(sid_a, 0).token
        scope_b = bus._tree.ensure_session(sid_b, 0).token

        # Subscribe with session scope
        bus.subscribe("run.completed.v1", stats_a.on_event, "stats-a",
                      scope=scope_a)
        bus.subscribe("run.completed.v1", stats_b.on_event, "stats-b",
                      scope=scope_b)

        # Publish to session A
        env_a = EventEnvelope(
            event_id=EventId.generate(),
            event_type=EventTypeName("run.completed.v1"),
            schema_version=SchemaVersion(1),
            occurred_at=datetime.now(timezone.utc),
            source=EventSource(process_id="test", component="runtime"),
            scope=scope_a,
            correlation_id=CorrelationId("c1"),
            causation_id=None,
            aggregate_id=AggregateId("r-a"),
            aggregate_version=AggregateVersion(1),
            payload=completed("r-a"),
        )
        bus.publish(env_a)

        assert len(stats_a.metrics) == 1, "Session A subscriber should receive"
        assert len(stats_b.metrics) == 0, "Session B subscriber must NOT receive"


class TestOutboxToBusIntegration:

    def test_outbox_write_then_decode_and_publish(
        self, registry, bus, trace_projection, db_path
    ):
        """Simulate the outbox→relay→bus path: write JSON, decode, publish."""
        from infrastructure.outbox.sqlite_store import SqliteOutboxStore

        sid = SessionId("s-test")
        bus.ensure_session(sid)

        outbox = SqliteOutboxStore(db_path, registry)

        # Install outbox tables
        import sqlite3
        conn = sqlite3.connect(db_path)
        SqliteOutboxStore.install(conn)
        conn.commit()

        # Write envelope to outbox (same transaction, simulating real UoW)
        env = _make_envelope(
            "run.completed.v1",
            completed("r-test", steps_taken=3, tokens_used=100),
        )
        conn.execute("BEGIN IMMEDIATE")
        outbox.append(conn, env)
        conn.commit()
        conn.close()

        # Simulate relay: claim, decode, publish
        records = outbox.claim_batch("worker-1", limit=10)
        assert len(records) == 1
        record = records[0]

        # Decode and publish (this is what _deliver does)
        decoded = registry.decode(record.payload_json)
        bus.subscribe("run.completed.v1", trace_projection.on_event, "trace")
        bus.publish(decoded)

        # Verify trace projection received
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        traces = conn2.execute(
            "SELECT * FROM session_trace_events WHERE event_type=?",
            ("run.completed.v1",)
        ).fetchall()
        conn2.close()
        assert len(traces) == 1

        # Mark delivered
        outbox.mark_delivered(record.event_id, "worker-1")
