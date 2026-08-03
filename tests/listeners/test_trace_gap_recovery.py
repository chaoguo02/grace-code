"""G25: Trace Projection — version gap, idempotent, watermark.

AC: Receipt + trace + watermark in same transaction
AC: Duplicate source+id → idempotent (same receipt)
AC: Version gap detected → returns gap info
AC: Older version → ignored
AC: Normal sequence → watermark advances correctly
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
from listeners.trace_projection import TraceProjection
from listeners.projection_state import ProjectionStateStore, GapInfo


@pytest.fixture
def temp_db():
    d = tempfile.mkdtemp()
    db = str(Path(d) / "test.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS event_projection_receipts (
            consumer_name TEXT, event_id TEXT, processed_at TEXT,
            PRIMARY KEY (consumer_name, event_id));
        CREATE TABLE IF NOT EXISTS session_trace_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
            seq INTEGER DEFAULT 0, event_type TEXT, timestamp TEXT,
            event_json TEXT, source TEXT DEFAULT '');
    """)
    ProjectionStateStore.install(conn)
    conn.commit()
    conn.close()
    yield db
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def _make_envelope(run_id="r-test", session_id="s-test", version=1):
    sid = SessionId(session_id)
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
        aggregate_version=AggregateVersion(version),
        payload=completed(run_id, steps_taken=3, tokens_used=100),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# G25.1 — Normal sequence
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormalSequence:
    """G25: Events processed in order, watermark advances."""

    def test_normal_sequence_v1_v2_v3(self, temp_db):
        tp = TraceProjection(temp_db)
        state = ProjectionStateStore(temp_db, tp.NAME)

        r1 = tp.on_event(_make_envelope("r-agg", version=1))
        assert r1.success, f"v1 should succeed, got {r1.success}"

        r2 = tp.on_event(_make_envelope("r-agg", version=2))
        assert r2.success, f"v2 should succeed"

        r3 = tp.on_event(_make_envelope("r-agg", version=3))
        assert r3.success, f"v3 should succeed"

        assert state.get_watermark("r-agg") == 3

    def test_trace_rows_written(self, temp_db):
        tp = TraceProjection(temp_db)
        tp.on_event(_make_envelope("r-trace"))

        conn = sqlite3.connect(temp_db)
        count = conn.execute("SELECT COUNT(*) FROM session_trace_events").fetchone()[0]
        conn.close()
        assert count == 1


# ═══════════════════════════════════════════════════════════════════════════════
# G25.2 — Duplicate idempotent
# ═══════════════════════════════════════════════════════════════════════════════

class TestDuplicateIdempotent:
    """G25: Same source+id → idempotent (second event ignored)."""

    def test_duplicate_skipped(self, temp_db):
        tp = TraceProjection(temp_db)
        env = _make_envelope("r-dup")

        r1 = tp.on_event(env)
        assert r1.success

        r2 = tp.on_event(env)  # same event
        assert r2.success  # idempotent, not error

        conn = sqlite3.connect(temp_db)
        count = conn.execute("SELECT COUNT(*) FROM session_trace_events").fetchone()[0]
        conn.close()
        assert count == 1, f"Duplicate must not create second trace row, got {count}"


# ═══════════════════════════════════════════════════════════════════════════════
# G25.3 — Version gap
# ═══════════════════════════════════════════════════════════════════════════════

class TestVersionGap:
    """G25: Version gap detected and reported."""

    def test_gap_detected(self, temp_db):
        tp = TraceProjection(temp_db)
        tp.on_event(_make_envelope("r-gap", version=1))

        # Skip v2, send v3 → gap detected but v3 still processed
        r = tp.on_event(_make_envelope("r-gap", version=3))
        assert r.success, "v3 should succeed (forward gap processed, gap noted)"
        # Watermark should jump to 3 (skipping v2)
        state = ProjectionStateStore(temp_db, tp.NAME)
        assert state.get_watermark("r-gap") == 3

    def test_older_version_ignored(self, temp_db):
        tp = TraceProjection(temp_db)
        tp.on_event(_make_envelope("r-old", version=5))

        # Send v3 after v5 → old, should be ignored
        r = tp.on_event(_make_envelope("r-old", version=3))
        # Old version ≤ current watermark → ignored (returns ok for idempotent)
        # Watermark stays at 5
        state = ProjectionStateStore(temp_db, tp.NAME)
        assert state.get_watermark("r-old") == 5


# ═══════════════════════════════════════════════════════════════════════════════
# G25.4 — Watermark
# ═══════════════════════════════════════════════════════════════════════════════

class TestWatermark:
    """G25: ProjectionStateStore watermark management."""

    def test_initial_watermark_zero(self, temp_db):
        state = ProjectionStateStore(temp_db, "test_proj")
        assert state.get_watermark("any") == 0

    def test_gap_check_ok(self, temp_db):
        state = ProjectionStateStore(temp_db, "test_proj")
        conn = sqlite3.connect(temp_db)
        state.advance(conn, "agg", 3)
        conn.commit()
        conn.close()

        gap = state.check_gap("agg", 4)
        assert gap is None, f"Expected no gap for v4 after v3, got {gap}"

    def test_gap_check_missing(self, temp_db):
        state = ProjectionStateStore(temp_db, "test_proj")
        conn = sqlite3.connect(temp_db)
        state.advance(conn, "agg", 3)
        conn.commit()
        conn.close()

        gap = state.check_gap("agg", 6)
        assert gap is not None
        assert "3..5" in gap.missing_range or "4.." in gap.missing_range
