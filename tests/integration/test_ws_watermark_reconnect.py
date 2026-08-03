"""G27: WS Watermark Reconnect — snapshot, high-watermark, terminal once.

AC: connect reads snapshot from trace
AC: high-watermark avoids gap between snapshot and live
AC: terminal fact shown only once
AC: disconnect removes subscription; 1000 reconnect no retained callbacks
AC: WS failure does not affect durable ACK
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
from listeners.ws_gateway import WsGateway
from listeners.trace_projection import TraceProjection
from server.ws.native_event_mapper import NativeEventMapper, WatermarkToken


@pytest.fixture
def temp_db():
    d = tempfile.mkdtemp()
    db = str(Path(d) / "test.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS session_trace_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
            seq INTEGER DEFAULT 0, event_type TEXT, timestamp TEXT,
            event_json TEXT, source TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS event_projection_receipts (
            consumer_name TEXT, event_id TEXT, processed_at TEXT,
            PRIMARY KEY (consumer_name, event_id));
        CREATE TABLE IF NOT EXISTS projection_watermarks (
            projection_name TEXT NOT NULL, aggregate_id TEXT NOT NULL,
            last_version INTEGER NOT NULL DEFAULT 0, updated_at TEXT,
            PRIMARY KEY (projection_name, aggregate_id));
    """)
    conn.commit()
    conn.close()
    yield db
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def _make_envelope(run_id="r-test", session_id="s-test", event_type="run.completed.v1"):
    sid = SessionId(session_id)
    return EventEnvelope(
        event_id=EventId.generate(),
        event_type=EventTypeName(event_type),
        schema_version=SchemaVersion(1),
        occurred_at=datetime.now(timezone.utc),
        source=EventSource(process_id="test", component="runtime"),
        scope=ScopeToken.session_scope(uuid.uuid4(), sid),
        correlation_id=CorrelationId("c1"),
        causation_id=None,
        aggregate_id=AggregateId(run_id),
        aggregate_version=AggregateVersion(1),
        payload=completed(run_id),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# G27.1 — Gateway subscribe/broadcast/disconnect
# ═══════════════════════════════════════════════════════════════════════════════

class TestGatewayLifecycle:
    """G27: subscribe, broadcast, disconnect, no leaks."""

    def test_subscribe_and_broadcast(self):
        gw = WsGateway()
        received = []

        gw.subscribe("s1", lambda m: received.append(m))
        gw.broadcast("s1", {"type": "test"})
        assert len(received) == 1

    def test_disconnect_removes_all(self):
        gw = WsGateway()
        received = []
        gw.subscribe("s1", lambda m: received.append(m))
        gw.disconnect_session("s1")
        gw.broadcast("s1", {"type": "test"})
        assert len(received) == 0

    def test_1000_reconnects_no_leak(self):
        """1000 subscribe/disconnect cycles — no retained callbacks."""
        gw = WsGateway()
        for _ in range(1000):
            cb = lambda m: None
            gw.subscribe("s1", cb)
            gw.unsubscribe("s1", cb)
        assert gw.subscriber_sessions == 0, (
            f"G27: 1000 reconnect must not leak subscribers, got {gw.subscriber_sessions}"
        )

    def test_broadcast_error_swallowed(self):
        """WS failure does NOT affect other subscribers or raise."""
        gw = WsGateway()
        received = []

        def _crash(msg):
            raise RuntimeError("WS disconnected!")

        def _good(msg):
            received.append(msg)

        gw.subscribe("s1", _crash)
        gw.subscribe("s1", _good)
        gw.broadcast("s1", {"type": "test"})
        assert len(received) == 1, "Good subscriber must still receive"


# ═══════════════════════════════════════════════════════════════════════════════
# G27.2 — Watermark + snapshot
# ═══════════════════════════════════════════════════════════════════════════════

class TestWatermarkSnapshot:
    """G27: connect reads snapshot, high-watermark."""

    def test_connect_reads_snapshot(self, temp_db):
        # Populate trace with some events
        tp = TraceProjection(temp_db)
        tp.on_event(_make_envelope("r1", "s-test", "run.completed.v1"))
        tp.on_event(_make_envelope("r2", "s-test", "run.completed.v1"))

        gw = WsGateway()
        mapper = NativeEventMapper(temp_db, gw)

        events, token = mapper.connect("s-test")
        assert len(events) == 2
        assert token.last_seq >= 2

    def test_terminal_sent_only_once(self, temp_db):
        mapper = NativeEventMapper(temp_db, WsGateway())

        assert not mapper.is_terminal_already_sent("run.completed.v1", "s1")
        mapper.mark_terminal_sent("run.completed.v1", "s1")
        assert mapper.is_terminal_already_sent("run.completed.v1", "s1")
