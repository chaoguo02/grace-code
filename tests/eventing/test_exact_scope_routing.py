"""G5: Exact Scope Routing — full ScopeToken equality, no catch-all.

Covers:
  - 3×3 routing matrix: GLOBAL/SESSION/TASK publisher × subscriber
  - GLOBAL subscriber does NOT receive SESSION/TASK events
  - SESSION subscriber does NOT receive GLOBAL or cross-session events
  - TASK subscriber does NOT receive GLOBAL/SESSION/cross-task events
  - scope=None removed — subscribe() requires explicit ScopeToken
  - Duplicate subscription rejection
  - Subscription close removes from subscriber count
  - Cross-global, cross-generation rejection
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from core.eventing.identifiers import (
    SessionId, TaskId, RunId, EventId, AggregateVersion,
)
from core.eventing.scope import ScopeKind, ScopeToken
from application.events.envelope import (
    EventEnvelope, EventTypeName, SchemaVersion, EventSource,
    CorrelationId, AggregateId,
)
from application.events.run_facts import RunSubmittedV1, completed
from eventing.scoped_bus import ScopedEventBus, _scope_matches
from eventing.subscription import Subscription, DuplicateSubscriptionError


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_envelope(event_type: str, scope: ScopeToken,
                   payload=None, session_id="s-test"):
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
        payload=payload or RunSubmittedV1(run_id=RunId("r1")),
    )


def _global_scope(tree) -> ScopeToken:
    return tree._root.token


def _session_scope(tree, session_id: str, generation: int = 0) -> ScopeToken:
    sid = SessionId(session_id)
    return tree.ensure_session(sid, generation).token


def _task_scope(tree, session_id: str, task_id: str, generation: int = 0) -> ScopeToken:
    sid = SessionId(session_id)
    tid = TaskId(task_id)
    return tree.ensure_task(sid, tid, generation).token


# ═══════════════════════════════════════════════════════════════════════════════
# G5.1 — 3×3 Routing Matrix
# ═══════════════════════════════════════════════════════════════════════════════

class TestExactScopeMatrix:
    """G5: GLOBAL↔GLOBAL, SESSION↔SESSION, TASK↔TASK only.  No cross-kind."""

    ROWS = ["GLOBAL", "SESSION", "TASK"]

    def test_global_event_to_global_subscriber(self):
        bus = ScopedEventBus()
        g_scope = _global_scope(bus._tree)
        s_scope = _session_scope(bus._tree, "s1")

        g_recv = []
        s_recv = []

        bus.subscribe("run.submitted.v1", lambda e: g_recv.append(e),
                      "g", scope=g_scope)
        bus.subscribe("run.submitted.v1", lambda e: s_recv.append(e),
                      "s", scope=s_scope)

        bus.publish(_make_envelope("run.submitted.v1", g_scope))

        assert len(g_recv) == 1, "GLOBAL subscriber must receive GLOBAL event"
        assert len(s_recv) == 0, "G5: SESSION subscriber must NOT receive GLOBAL event"

    def test_session_event_to_session_subscriber(self):
        bus = ScopedEventBus()
        g_scope = _global_scope(bus._tree)
        s_scope = _session_scope(bus._tree, "s1")

        g_recv = []
        s_recv = []

        bus.subscribe("run.submitted.v1", lambda e: g_recv.append(e),
                      "g", scope=g_scope)
        bus.subscribe("run.submitted.v1", lambda e: s_recv.append(e),
                      "s", scope=s_scope)

        bus.publish(_make_envelope("run.submitted.v1", s_scope))

        assert len(s_recv) == 1, "SESSION subscriber must receive SESSION event"
        assert len(g_recv) == 0, "G5: GLOBAL subscriber must NOT receive SESSION event"

    def test_task_event_to_task_subscriber(self):
        bus = ScopedEventBus()
        g_scope = _global_scope(bus._tree)
        s_scope = _session_scope(bus._tree, "s1")
        t_scope = _task_scope(bus._tree, "s1", "t1")

        g_recv, s_recv, t_recv = [], [], []

        bus.subscribe("run.submitted.v1", lambda e: g_recv.append(e),
                      "g", scope=g_scope)
        bus.subscribe("run.submitted.v1", lambda e: s_recv.append(e),
                      "s", scope=s_scope)
        bus.subscribe("run.submitted.v1", lambda e: t_recv.append(e),
                      "t", scope=t_scope)

        bus.publish(_make_envelope("run.submitted.v1", t_scope))

        assert len(t_recv) == 1, "TASK subscriber must receive TASK event"
        assert len(s_recv) == 0, "G5: SESSION subscriber must NOT receive TASK event"
        assert len(g_recv) == 0, "G5: GLOBAL subscriber must NOT receive TASK event"

    def test_global_event_only_to_global(self):
        """GLOBAL event → only exact GLOBAL subscribers."""
        bus = ScopedEventBus()
        g1 = _global_scope(bus._tree)
        s1 = _session_scope(bus._tree, "s1")
        t1 = _task_scope(bus._tree, "s1", "t1")

        r_g, r_s, r_t = [], [], []
        bus.subscribe("run.submitted.v1", lambda e: r_g.append(e), "g", scope=g1)
        bus.subscribe("run.submitted.v1", lambda e: r_s.append(e), "s", scope=s1)
        bus.subscribe("run.submitted.v1", lambda e: r_t.append(e), "t", scope=t1)

        bus.publish(_make_envelope("run.submitted.v1", g1))
        assert len(r_g) == 1
        assert len(r_s) == 0
        assert len(r_t) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# G5.2 — No cross-session / cross-task delivery
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoCrossScope:
    """G5: Events never leak between sessions or tasks."""

    def test_session_a_event_not_to_session_b(self):
        bus = ScopedEventBus()
        sa = _session_scope(bus._tree, "s-a")
        sb = _session_scope(bus._tree, "s-b")

        ra, rb = [], []
        bus.subscribe("run.submitted.v1", lambda e: ra.append(e), "a", scope=sa)
        bus.subscribe("run.submitted.v1", lambda e: rb.append(e), "b", scope=sb)

        bus.publish(_make_envelope("run.submitted.v1", sa))
        assert len(ra) == 1
        assert len(rb) == 0, "G5: Session B must NOT receive Session A events"

    def test_task_a_event_not_to_task_b(self):
        bus = ScopedEventBus()
        ta = _task_scope(bus._tree, "s1", "t-a")
        tb = _task_scope(bus._tree, "s1", "t-b")

        ra, rb = [], []
        bus.subscribe("run.submitted.v1", lambda e: ra.append(e), "a", scope=ta)
        bus.subscribe("run.submitted.v1", lambda e: rb.append(e), "b", scope=tb)

        bus.publish(_make_envelope("run.submitted.v1", ta))
        assert len(ra) == 1
        assert len(rb) == 0, "G5: Task B must NOT receive Task A events"

    def test_task_event_not_to_parent_session(self):
        bus = ScopedEventBus()
        s_scope = _session_scope(bus._tree, "s1")
        t_scope = _task_scope(bus._tree, "s1", "t1")

        sr, tr = [], []
        bus.subscribe("run.submitted.v1", lambda e: sr.append(e), "s", scope=s_scope)
        bus.subscribe("run.submitted.v1", lambda e: tr.append(e), "t", scope=t_scope)

        bus.publish(_make_envelope("run.submitted.v1", t_scope))
        assert len(tr) == 1
        assert len(sr) == 0, "G5: SESSION subscriber must NOT receive TASK event"

    def test_session_event_not_to_child_task(self):
        bus = ScopedEventBus()
        s_scope = _session_scope(bus._tree, "s1")
        t_scope = _task_scope(bus._tree, "s1", "t1")

        sr, tr = [], []
        bus.subscribe("run.submitted.v1", lambda e: sr.append(e), "s", scope=s_scope)
        bus.subscribe("run.submitted.v1", lambda e: tr.append(e), "t", scope=t_scope)

        bus.publish(_make_envelope("run.submitted.v1", s_scope))
        assert len(sr) == 1
        assert len(tr) == 0, "G5: TASK subscriber must NOT receive SESSION event"


# ═══════════════════════════════════════════════════════════════════════════════
# G5.3 — Cross-global (different global_id) rejected
# ═══════════════════════════════════════════════════════════════════════════════

class TestCrossGlobal:
    """G5: Different global_id → different scopes, no cross-delivery."""

    def test_different_global_ids_not_equal(self):
        bus1 = ScopedEventBus()
        bus2 = ScopedEventBus()

        g1 = _global_scope(bus1._tree)
        g2 = _global_scope(bus2._tree)

        assert g1 != g2, "Different global_id → different ScopeToken"
        assert not _scope_matches(g1, g2), "G5: cross-global must not match"


# ═══════════════════════════════════════════════════════════════════════════════
# G5.4 — Cross-generation rejected
# ═══════════════════════════════════════════════════════════════════════════════

class TestCrossGeneration:
    """G5: Different generation → different scope, no match."""

    def test_different_generation_not_equal(self):
        bus = ScopedEventBus()
        s1 = _session_scope(bus._tree, "s1", generation=1)
        s2 = _session_scope(bus._tree, "s1", generation=2)

        assert s1 != s2, "Different generation → different ScopeToken"
        assert not _scope_matches(s1, s2), "G5: cross-generation must not match"

    def test_cross_generation_no_delivery(self):
        """Gen 2 subscriber does NOT receive gen 1 events, and vice versa."""
        bus = ScopedEventBus()
        # Create both scopes first, then subscribe
        s_gen1 = _session_scope(bus._tree, "s1", generation=1)

        # Subscribe at gen 1
        r1 = []
        bus.subscribe("run.submitted.v1", lambda e: r1.append(e), "a", scope=s_gen1)

        # Publish at gen 1 — works because it's the current generation
        bus.publish(_make_envelope("run.submitted.v1", s_gen1))
        assert len(r1) == 1, "Gen 1 subscriber must receive gen 1 event"

        # Now bump to gen 2 — old gen 1 subscriber scope won't match gen 2 events
        s_gen2 = _session_scope(bus._tree, "s1", generation=2)
        r2 = []
        bus.subscribe("run.submitted.v1", lambda e: r2.append(e), "b", scope=s_gen2)

        # Publish at gen 2 — only gen 2 subscriber receives
        bus.publish(_make_envelope("run.submitted.v1", s_gen2))
        assert len(r2) == 1, "Gen 2 subscriber must receive gen 2 event"
        assert len(r1) == 1, "G5: Gen 1 subscriber must NOT receive gen 2 event (no bubbling)"


# ═══════════════════════════════════════════════════════════════════════════════
# G5.5 — scope=None removed
# ═══════════════════════════════════════════════════════════════════════════════

class TestScopeRequired:
    """G5: subscribe() MUST have an explicit ScopeToken."""

    def test_subscribe_without_scope_raises(self):
        bus = ScopedEventBus()
        bus.ensure_session(SessionId("s1"))
        with pytest.raises(TypeError):
            bus.subscribe("run.submitted.v1", lambda e: None, "test")  # no scope

    def test_subscribe_with_scope_ok(self):
        bus = ScopedEventBus()
        bus.ensure_session(SessionId("s1"))
        scope = _session_scope(bus._tree, "s1")
        sub = bus.subscribe("run.submitted.v1", lambda e: None, "test", scope=scope)
        assert isinstance(sub, Subscription)
        assert sub.scope == scope


# ═══════════════════════════════════════════════════════════════════════════════
# G5.6 — Duplicate subscription rejection
# ═══════════════════════════════════════════════════════════════════════════════

class TestDuplicateSubscription:
    """G5: Same (event_type, subscriber_id, scope) → DuplicateSubscriptionError."""

    def test_duplicate_same_scope_raises(self):
        bus = ScopedEventBus()
        scope = _session_scope(bus._tree, "s1")
        bus.subscribe("run.submitted.v1", lambda e: None, "dup", scope=scope)
        with pytest.raises(DuplicateSubscriptionError, match="Duplicate"):
            bus.subscribe("run.submitted.v1", lambda e: None, "dup", scope=scope)

    def test_same_id_different_event_type_ok(self):
        bus = ScopedEventBus()
        scope = _session_scope(bus._tree, "s1")
        bus.subscribe("run.submitted.v1", lambda e: None, "s", scope=scope)
        bus.subscribe("run.completed.v1", lambda e: None, "s", scope=scope)
        assert bus.subscriber_count == 2

    def test_same_event_different_id_ok(self):
        bus = ScopedEventBus()
        scope = _session_scope(bus._tree, "s1")
        bus.subscribe("run.submitted.v1", lambda e: None, "a", scope=scope)
        bus.subscribe("run.submitted.v1", lambda e: None, "b", scope=scope)
        assert bus.subscriber_count == 2


# ═══════════════════════════════════════════════════════════════════════════════
# G5.7 — Subscription close removes from count
# ═══════════════════════════════════════════════════════════════════════════════

class TestSubscriptionClose:
    """G5: Closing a subscription immediately removes it from count."""

    def test_close_reduces_subscriber_count(self):
        bus = ScopedEventBus()
        scope = _session_scope(bus._tree, "s1")
        sub = bus.subscribe("run.submitted.v1", lambda e: None, "c", scope=scope)
        assert bus.subscriber_count == 1
        sub.close()
        assert bus.subscriber_count == 0, "G5: closed subscription must not count"

    def test_closed_subscription_not_delivered(self):
        bus = ScopedEventBus()
        scope = _session_scope(bus._tree, "s1")
        received = []
        sub = bus.subscribe("run.submitted.v1", lambda e: received.append(e),
                            "c", scope=scope)
        sub.close()
        bus.publish(_make_envelope("run.submitted.v1", scope))
        assert len(received) == 0, "G5: closed subscription must not receive events"


# ═══════════════════════════════════════════════════════════════════════════════
# G5.8 — Subscription with scope has correct identity
# ═══════════════════════════════════════════════════════════════════════════════

class TestSubscriptionIdentity:
    """G5: Subscription identity includes scope identity."""

    def test_scope_accessible_on_subscription(self):
        bus = ScopedEventBus()
        scope = _session_scope(bus._tree, "s1")
        sub = bus.subscribe("run.submitted.v1", lambda e: None, "test", scope=scope)
        assert sub.scope is not None
        assert sub.scope.kind == ScopeKind.SESSION

    def test_different_scope_different_subscriptions(self):
        bus = ScopedEventBus()
        s1 = _session_scope(bus._tree, "s1")
        s2 = _session_scope(bus._tree, "s2")
        sub1 = bus.subscribe("run.submitted.v1", lambda e: None, "x", scope=s1)
        sub2 = bus.subscribe("run.submitted.v1", lambda e: None, "x", scope=s2)
        # Same subscriber_id but different scope → OK (not duplicates)
        assert sub1 is not sub2
        assert bus.subscriber_count == 2

    def test_subscription_repr(self):
        bus = ScopedEventBus()
        scope = _session_scope(bus._tree, "s1")
        sub = bus.subscribe("run.submitted.v1", lambda e: None, "rep", scope=scope)
        r = repr(sub)
        assert "run.submitted.v1" in r
        assert "rep" in r
