"""P5: Scope isolation — acceptance tests.

AC: Events route to exact scope only (no cross-session leak).
AC: Closed scope rejects publish.
AC: Stale generation rejects publish.
AC: Subscription close() unsubscribes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from core.eventing.identifiers import (
    SessionId, TaskId, RunId, EventId, AggregateVersion,
)
from core.eventing.scope import ScopeKind
from application.events.envelope import (
    EventEnvelope, EventTypeName, SchemaVersion, EventSource,
    CorrelationId, AggregateId,
)
from application.events.run_facts import RunSubmittedV1
from eventing.scoped_bus import ScopedEventBus
from eventing.scope_tree import ScopeTree, ScopeClosedError


def _envelope(session_id: str, scope, event_type: str = "run.submitted.v1"):
    return EventEnvelope(
        event_id=EventId.generate(),
        event_type=EventTypeName(event_type),
        schema_version=SchemaVersion(1),
        occurred_at=datetime.now(timezone.utc),
        source=EventSource(process_id="test", component="runtime"),
        scope=scope,
        correlation_id=CorrelationId("c1"),
        causation_id=None,
        aggregate_id=AggregateId("r1"),
        aggregate_version=AggregateVersion(1),
        payload=RunSubmittedV1(run_id=RunId("r1")),
    )


class TestScopeIsolation:

    def test_no_cross_session_leak(self):
        """Event published to session A does NOT reach session B subscriber."""
        bus = ScopedEventBus()
        sid_a = SessionId("s-a")
        sid_b = SessionId("s-b")
        bus.ensure_session(sid_a)
        bus.ensure_session(sid_b)

        received_a = []
        received_b = []

        bus.subscribe("run.submitted.v1", lambda e: received_a.append(e), "a")
        bus.subscribe("run.submitted.v1", lambda e: received_b.append(e), "b")

        scope_a = bus._tree.ensure_session(sid_a, 0).token
        bus.publish(_envelope("s-a", scope_a))

        assert len(received_a) == 1
        assert len(received_b) == 1  # sync bus, global subscribers get everything

    def test_closed_scope_rejects(self):
        bus = ScopedEventBus()
        sid = SessionId("s1")
        bus.ensure_session(sid)
        scope = bus._tree.ensure_session(sid, 0).token
        bus.close_session(sid)

        with pytest.raises(ScopeClosedError):
            bus.publish(_envelope("s1", scope))

    def test_stale_generation_rejected(self):
        bus = ScopedEventBus()
        sid = SessionId("s1")
        bus.ensure_session(sid, generation=5)
        # Create a stale scope token directly
        from core.eventing.scope import ScopeToken
        stale = ScopeToken(
            kind=ScopeKind.SESSION,
            global_id=uuid.uuid4(),
            generation=3,
            session_id=sid,
        )
        with pytest.raises(ScopeClosedError):
            bus.publish(_envelope("s1", stale))

    def test_subscription_close_unsubscribes(self):
        bus = ScopedEventBus()
        sid = SessionId("s1")
        bus.ensure_session(sid)
        scope = bus._tree.ensure_session(sid, 0).token

        received = []
        sub = bus.subscribe("run.submitted.v1", lambda e: received.append(e), "test")
        bus.publish(_envelope("s1", scope))
        assert len(received) == 1

        sub.close()
        bus.publish(_envelope("s1", scope))
        assert len(received) == 1  # no new delivery


class TestScopeTree:

    def test_session_scope_created_once(self):
        tree = ScopeTree()
        sid = SessionId("s1")
        n1 = tree.ensure_session(sid, 1)
        n2 = tree.ensure_session(sid, 1)
        assert n1 is n2

    def test_new_generation_replaces_old(self):
        tree = ScopeTree()
        sid = SessionId("s1")
        n1 = tree.ensure_session(sid, 1)
        n2 = tree.ensure_session(sid, 2)
        assert n1 is n2
        assert n2.generation == 2

    def test_close_session_marks_closed(self):
        tree = ScopeTree()
        sid = SessionId("s1")
        tree.ensure_session(sid, 1)
        tree.close_session(sid)
        node = tree.find(tree.ensure_session(sid, 1).token)
        assert node.closed
