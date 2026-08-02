"""P15: Trace Projection — acceptance tests."""

import sqlite3
import uuid
from datetime import datetime, timezone

import pytest

from application.events.envelope import EventEnvelope, EventTypeName, SchemaVersion, EventSource, CorrelationId, AggregateId
from application.events.run_facts import RunSubmittedV1
from core.eventing.identifiers import EventId, RunId, AggregateVersion, SessionId
from core.eventing.scope import ScopeToken
from listeners.trace_projection import TraceProjection


def _envelope():
    sid = SessionId("s1")
    return EventEnvelope(
        event_id=EventId.generate(), event_type=EventTypeName("run.submitted.v1"),
        schema_version=SchemaVersion(1), occurred_at=datetime.now(timezone.utc),
        source=EventSource("test", "runtime"), correlation_id=CorrelationId("c1"),
        causation_id=None, aggregate_id=AggregateId("r1"),
        aggregate_version=AggregateVersion(1),
        scope=ScopeToken.session_scope(uuid.uuid4(), sid),
        payload=RunSubmittedV1(run_id=RunId("r1")),
    )


class TestTraceProjection:
    def test_idempotent(self, tmp_path):
        db = str(tmp_path / "test.db")
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS event_projection_receipts
                (consumer_name TEXT, event_id TEXT, processed_at TEXT, PRIMARY KEY(consumer_name, event_id));
            CREATE TABLE IF NOT EXISTS session_trace_events
                (id INTEGER PRIMARY KEY, session_id TEXT, seq INTEGER, event_type TEXT, timestamp TEXT, event_json TEXT, source TEXT);
        """)
        conn.commit(); conn.close()

        proj = TraceProjection(db)
        env = _envelope()
        r1 = proj.on_event(env)
        r2 = proj.on_event(env)
        assert r1.success
        assert r2.success  # idempotent — second call also returns ok
