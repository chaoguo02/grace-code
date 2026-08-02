"""P15: Trace Projection — comprehensive tests.

AC: First projection creates trace row + receipt in same tx.
AC: Second projection idempotent — no duplicate trace.
AC: Receipt+Trace in same transaction (rollback on failure).
AC: Different consumers can project same event independently.
"""

import sqlite3
import uuid
from datetime import datetime, timezone

import pytest

from application.events.envelope import EventEnvelope, EventTypeName, SchemaVersion, EventSource, CorrelationId, AggregateId
from application.events.run_facts import RunSubmittedV1
from core.eventing.identifiers import EventId, RunId, AggregateVersion, SessionId
from core.eventing.scope import ScopeToken
from listeners.trace_projection import TraceProjection


def _envelope(eid: str | None = None, sid: str = "s1"):
    s = SessionId(sid)
    return EventEnvelope(
        event_id=EventId(uuid.UUID(eid)) if eid else EventId.generate(),
        event_type=EventTypeName("run.submitted.v1"),
        schema_version=SchemaVersion(1), occurred_at=datetime.now(timezone.utc),
        source=EventSource("test", "runtime"), correlation_id=CorrelationId("c1"),
        causation_id=None, aggregate_id=AggregateId("r1"),
        aggregate_version=AggregateVersion(1),
        scope=ScopeToken.session_scope(uuid.uuid4(), s),
        payload=RunSubmittedV1(run_id=RunId("r1")),
    )


def _setup_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS event_projection_receipts
            (consumer_name TEXT, event_id TEXT, processed_at TEXT, PRIMARY KEY(consumer_name, event_id));
        CREATE TABLE IF NOT EXISTS session_trace_events
            (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, seq INTEGER, event_type TEXT, timestamp TEXT, event_json TEXT, source TEXT);
    """)
    conn.commit()
    conn.close()


def _trace_count(path: str) -> int:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    n = conn.execute("SELECT COUNT(*) as c FROM session_trace_events").fetchone()["c"]
    conn.close()
    return n


def _receipt_count(path: str) -> int:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    n = conn.execute("SELECT COUNT(*) as c FROM event_projection_receipts").fetchone()["c"]
    conn.close()
    return n


class TestTraceProjection:

    def test_first_projection_creates_trace(self, tmp_path):
        db = str(tmp_path / "test.db")
        _setup_db(db)
        proj = TraceProjection(db)
        r = proj.on_event(_envelope())
        assert r.success
        assert _trace_count(db) == 1

    def test_second_projection_idempotent(self, tmp_path):
        db = str(tmp_path / "test.db")
        _setup_db(db)
        proj = TraceProjection(db)
        env = _envelope()
        r1 = proj.on_event(env)
        r2 = proj.on_event(env)
        assert r1.success and r2.success
        assert _trace_count(db) == 1  # no duplicate
        assert _receipt_count(db) == 1

    def test_receipt_and_trace_in_same_transaction(self, tmp_path):
        """If trace insert fails after receipt, both roll back."""
        db = str(tmp_path / "test.db")
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS event_projection_receipts
                (consumer_name TEXT, event_id TEXT, processed_at TEXT, PRIMARY KEY(consumer_name, event_id));
        """)
        # Note: no session_trace_events table — should trigger ROLLBACK
        conn.commit(); conn.close()

        proj = TraceProjection(db)
        r = proj.on_event(_envelope())
        # Should fail (no trace table), receipt should also be rolled back
        assert not r.success
        assert _receipt_count(db) == 0

    def test_different_consumers_independent(self, tmp_path):
        db = str(tmp_path / "test.db")
        _setup_db(db)
        proj1 = TraceProjection(db)
        proj1.NAME = "trace_v1"
        proj2 = TraceProjection(db)
        proj2.NAME = "trace_v2"

        env = _envelope()
        assert proj1.on_event(env).success
        assert proj2.on_event(env).success
        assert _receipt_count(db) == 2  # two consumers, two receipts
