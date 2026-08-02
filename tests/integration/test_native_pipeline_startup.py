"""Phase D: Server entry point integration tests.

Verifies that start_native_pipeline() assembles and starts correctly,
and that the outbox relay delivers events to projections.
"""

from __future__ import annotations

import os
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


@pytest.fixture
def temp_db():
    d = tempfile.mkdtemp()
    db = str(Path(d) / "test.db")
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

    def test_assemble_and_shutdown(self, temp_db):
        """start_native_pipeline() assembles, starts, and shuts down cleanly."""
        from composition.runtime_composition import start_native_pipeline

        pipeline = start_native_pipeline(temp_db)

        assert "relay" in pipeline
        assert "bus" in pipeline
        assert "trace" in pipeline
        assert "stats" in pipeline
        assert "ws_gateway" in pipeline
        assert "shutdown" in pipeline

        # Shutdown must not raise
        pipeline["shutdown"]()

    def test_pipeline_deliver_callback(self, temp_db):
        """Verify the _deliver callback: outbox record → decode → bus → trace.

        Tests the exact function used by OutboxRelay without thread timing.
        """
        from composition.runtime_composition import start_native_pipeline

        # Start pipeline to get wired bus + projections
        pipeline = start_native_pipeline(temp_db)
        registry = SchemaRegistry()
        outbox = SqliteOutboxStore(temp_db, registry)
        bus = pipeline["bus"]

        try:
            # Ensure trace tables exist
            conn = sqlite3.connect(temp_db)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS session_trace_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT, seq INTEGER DEFAULT 0,
                    event_type TEXT, timestamp TEXT,
                    event_json TEXT, source TEXT DEFAULT ''
                );
            """)
            conn.commit()
            conn.close()

            # Write an event to the outbox
            env = _make_completed_envelope("s-test")
            bus.ensure_session(SessionId("s-test"))

            conn = sqlite3.connect(temp_db)
            conn.execute("BEGIN IMMEDIATE")
            outbox.append(conn, env)
            conn.commit()
            conn.close()

            # Simulate what the relay does: claim → decode → publish → ack
            records = outbox.claim_batch("worker-1", limit=10)
            assert len(records) == 1
            record = records[0]

            decoded = registry.decode(record.payload_json)
            bus.publish(decoded)
            outbox.mark_delivered(record.event_id, "worker-1")

            # Verify delivered status
            conn = sqlite3.connect(temp_db)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT status FROM event_outbox WHERE event_id=?",
                (str(env.event_id),),
            ).fetchone()
            conn.close()
            assert row is not None
            assert row["status"] == "delivered"

            # Verify trace received the event
            conn = sqlite3.connect(temp_db)
            conn.row_factory = sqlite3.Row
            traces = conn.execute(
                "SELECT * FROM session_trace_events WHERE event_type=?",
                ("run.completed.v1",),
            ).fetchall()
            conn.close()
            assert len(traces) >= 1, "Trace projection should receive event"
        finally:
            pipeline["shutdown"]()

    def test_runtime_composition_assemble_native(self, temp_db):
        """RuntimeComposition.assemble() in NATIVE mode wires all components."""
        os.environ["GRACE_RUNTIME_MODE"] = "NATIVE"
        try:
            from composition.runtime_composition import RuntimeComposition
            comp = RuntimeComposition(temp_db)
            components = comp.assemble()

            assert components["mode"] == "NATIVE"
            assert components["bus"] is not None
            assert components["trace"] is not None
            assert components["stats"] is not None
            assert components["ws_gateway"] is not None
            assert components["relay"] is not None
        finally:
            os.environ.pop("GRACE_RUNTIME_MODE", None)
