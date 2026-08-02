"""G8: ProjectionDispatcher — typed dispatch, required vs best-effort.

AC: required projection failure → Retryable (not Delivered)
AC: best-effort failure → Delivered with error count
AC: unknown schema/version → Permanent
AC: multi-projection mixed outcomes
AC: no catch-all — must specify explicit event_types
AC: no Runtime/Command/Coordinator imports
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from core.eventing.identifiers import SessionId, RunId, EventId, AggregateVersion
from core.eventing.scope import ScopeToken
from application.events.envelope import (
    EventEnvelope, EventTypeName, SchemaVersion, EventSource,
    CorrelationId, AggregateId,
)
from application.events.run_facts import completed
from listeners.delivery import (
    Delivered,
    RetryableDeliveryFailure,
    PermanentDeliveryFailure,
    ProjectionReceipt,
    DeliveryOutcome,
    merge_receipts,
)
from listeners.projection_runner import ProjectionDispatcher, ProjectionRunner


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_envelope(event_type: str = "run.completed.v1", run_id: str = "r-test"):
    sid = SessionId("s-test")
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


def _ok_receipt(name: str, event_id: str = "e-1") -> ProjectionReceipt:
    return ProjectionReceipt(projection_name=name, event_id=event_id, success=True)


def _fail_receipt(name: str, event_id: str = "e-1", error: str = "DB down") -> ProjectionReceipt:
    return ProjectionReceipt(projection_name=name, event_id=event_id, success=False, error=error)


# ═══════════════════════════════════════════════════════════════════════════════
# G8.1 — DeliveryOutcome merge logic
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeliveryOutcome:
    """G8: merge_receipts produces correct outcomes."""

    def test_all_required_ok_delivers(self):
        result = merge_receipts(
            [_ok_receipt("trace"), _ok_receipt("stats")],
            required_names={"trace", "stats"},
            best_effort_names=set(),
        )
        assert isinstance(result, Delivered), f"Expected Delivered, got {type(result).__name__}"
        assert result.best_effort_errors == 0

    def test_required_failure_retryable(self):
        result = merge_receipts(
            [_ok_receipt("trace"), _fail_receipt("stats")],
            required_names={"trace", "stats"},
            best_effort_names=set(),
        )
        assert isinstance(result, RetryableDeliveryFailure), (
            f"G8 FAIL: required projection failure must be Retryable, "
            f"got {type(result).__name__}"
        )

    def test_best_effort_failure_delivers_with_error_count(self):
        result = merge_receipts(
            [_ok_receipt("trace"), _fail_receipt("ws_gateway")],
            required_names={"trace"},
            best_effort_names={"ws_gateway"},
        )
        assert isinstance(result, Delivered), (
            f"G8 FAIL: best-effort failure should still deliver, "
            f"got {type(result).__name__}"
        )
        assert result.best_effort_errors == 1

    def test_required_permanent_failure(self):
        result = merge_receipts(
            [_ok_receipt("trace"),
             _fail_receipt("audit", error="permanent: unknown schema")],
            required_names={"trace", "audit"},
            best_effort_names=set(),
        )
        assert isinstance(result, PermanentDeliveryFailure), (
            f"Expected Permanent, got {type(result).__name__}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# G8.2 — ProjectionDispatcher registration
# ═══════════════════════════════════════════════════════════════════════════════

class TestProjectionRegistration:
    """G8: explicit event_types required — no catch-all."""

    def test_empty_event_types_raises(self):
        dispatcher = ProjectionDispatcher()
        with pytest.raises(ValueError, match="event_types"):
            dispatcher.register("trace", lambda e: None, event_types=())

    def test_duplicate_name_raises(self):
        dispatcher = ProjectionDispatcher()
        dispatcher.register("trace", lambda e: None, event_types=("run.completed.v1",))
        with pytest.raises(ValueError, match="already registered"):
            dispatcher.register("trace", lambda e: None, event_types=("run.started.v1",))

    def test_different_names_same_event_type_ok(self):
        dispatcher = ProjectionDispatcher()
        dispatcher.register("trace", lambda e: _ok_receipt("trace"),
                            event_types=("run.completed.v1",))
        dispatcher.register("stats", lambda e: _ok_receipt("stats"),
                            event_types=("run.completed.v1",))
        assert dispatcher.entry_count == 2

    def test_backward_compat_alias(self):
        """ProjectionRunner is an alias for ProjectionDispatcher."""
        runner = ProjectionRunner()
        assert isinstance(runner, ProjectionDispatcher)


# ═══════════════════════════════════════════════════════════════════════════════
# G8.3 — Dispatch: required failure → Retryable
# ═══════════════════════════════════════════════════════════════════════════════

class TestDispatchRequiredFailure:
    """G8: When a required projection fails, the outcome is NOT Delivered."""

    def test_required_failure_returns_retryable(self):
        dispatcher = ProjectionDispatcher()

        def failing_trace(envelope):
            raise RuntimeError("trace DB connection lost")

        def ok_stats(envelope):
            return _ok_receipt("stats")

        dispatcher.register("trace", failing_trace, required=True,
                            event_types=("run.completed.v1",))
        dispatcher.register("stats", ok_stats, required=True,
                            event_types=("run.completed.v1",))

        env = _make_envelope()
        outcome = dispatcher.dispatch(env)

        assert isinstance(outcome, RetryableDeliveryFailure), (
            f"G8 BEFORE: required projection failure MUST be Retryable, "
            f"got {type(outcome).__name__} (would be false ACK if Delivered)"
        )

    def test_best_effort_failure_still_delivers(self):
        dispatcher = ProjectionDispatcher()

        def ok_trace(envelope):
            return _ok_receipt("trace")

        def failing_ws(envelope):
            raise RuntimeError("WS disconnected")

        dispatcher.register("trace", ok_trace, required=True,
                            event_types=("run.completed.v1",))
        dispatcher.register("ws_gateway", failing_ws, required=False,
                            event_types=("run.completed.v1",))

        env = _make_envelope()
        outcome = dispatcher.dispatch(env)

        assert isinstance(outcome, Delivered), (
            f"G8: best-effort failure should NOT block delivery, "
            f"got {type(outcome).__name__}"
        )
        assert outcome.best_effort_errors == 1


# ═══════════════════════════════════════════════════════════════════════════════
# G8.4 — Unknown schema → Permanent
# ═══════════════════════════════════════════════════════════════════════════════

class TestUnknownSchemaPermanent:
    """G8: Unregistered event type → PermanentDeliveryFailure."""

    def test_dispatch_unknown_returns_permanent(self):
        dispatcher = ProjectionDispatcher()
        outcome = dispatcher.dispatch_unknown("run.completed.v99", "e-1")
        assert isinstance(outcome, PermanentDeliveryFailure)

    def test_unregistered_event_type_no_matching_projection(self):
        dispatcher = ProjectionDispatcher()
        dispatcher.register("trace", lambda e: _ok_receipt("trace"),
                            event_types=("run.completed.v1",))

        # Send a different event type that no projection handles
        env = _make_envelope(event_type="run.started.v1")
        outcome = dispatcher.dispatch(env)

        # No projection registered → Delivered with empty receipts (not an error)
        assert isinstance(outcome, Delivered)
        assert len(outcome.receipts) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# G8.5 — Mixed outcomes
# ═══════════════════════════════════════════════════════════════════════════════

class TestMixedOutcomes:
    """G8: Multiple projections with mixed required + best-effort."""

    def test_mixed_required_and_best_effort_all_ok(self):
        dispatcher = ProjectionDispatcher()
        dispatcher.register("trace", lambda e: _ok_receipt("trace"), required=True,
                            event_types=("run.completed.v1",))
        dispatcher.register("stats", lambda e: _ok_receipt("stats"), required=True,
                            event_types=("run.completed.v1",))
        dispatcher.register("ws", lambda e: _ok_receipt("ws"), required=False,
                            event_types=("run.completed.v1",))

        outcome = dispatcher.dispatch(_make_envelope())
        assert isinstance(outcome, Delivered)
        assert outcome.best_effort_errors == 0
        assert len(outcome.receipts) == 3

    def test_no_catch_all_in_stats(self):
        """Stats must not handle events it doesn't explicitly register for."""
        dispatcher = ProjectionDispatcher()
        dispatcher.register("stats", lambda e: _ok_receipt("stats"),
                            event_types=("run.completed.v1",))

        # Send a different event
        env = _make_envelope(event_type="tool.executed.v1")
        outcome = dispatcher.dispatch(env)

        # No projection registered for tool.executed.v1 → Delivered with empty
        assert isinstance(outcome, Delivered)
        assert len(outcome.receipts) == 0, (
            "G8: projection must not receive unregistered event types"
        )

    def test_dispatcher_counts(self):
        dispatcher = ProjectionDispatcher()
        dispatcher.register("trace", lambda e: _ok_receipt("t"),
                            required=True,
                            event_types=("run.completed.v1", "run.started.v1"))
        dispatcher.register("ws", lambda e: _ok_receipt("w"),
                            required=False,
                            event_types=("run.completed.v1",))

        assert dispatcher.entry_count == 2
        assert dispatcher.required_count == 1
        assert dispatcher.best_effort_count == 1


# ═══════════════════════════════════════════════════════════════════════════════
# G8.6 — Import boundary
# ═══════════════════════════════════════════════════════════════════════════════

class TestImportBoundary:
    """G8: ProjectionDispatcher does NOT import Runtime/Command/Coordinator."""

    def test_no_runtime_imports(self):
        import ast
        import listeners.projection_runner as mod
        with open(mod.__file__, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, 'module', '') or ''
                if any(f in module for f in ('runtime_core', 'application.coordinators',
                                              'server.commands', 'agent')):
                    names = [n.name for n in getattr(node, 'names', [])]
                    pytest.fail(
                        f"G8 FAIL: projection_runner.py imports {module} "
                        f"(names: {names})"
                    )
