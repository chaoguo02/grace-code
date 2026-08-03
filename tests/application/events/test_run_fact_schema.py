"""P2: Run fact schema — acceptance tests.

AC: All payloads are frozen dataclasses with independent fields.
AC: Envelope canonical_json preserves type semantics.
AC: No dict passthrough — each terminal status gets its own class.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from core.eventing.identifiers import (
    SessionId, RunId, EventId, AggregateVersion,
)
from core.eventing.scope import ScopeToken

from application.events.envelope import (
    EventEnvelope, EventTypeName, SchemaVersion, EventSource,
    CorrelationId, AggregateId,
)
from application.events.run_facts import (
    RunTerminalStatus,
    RunSubmittedV1, RunStartedV1,
    RunCompletedV1, RunFailedV1, RunCancelledV1,
    RunBlockedV1, RunGaveUpV1,
    submitted, started, completed, failed, cancelled, blocked, gave_up,
)


# ── Payload classes ─────────────────────────────────────────────────────────

class TestRunFacts:

    def test_all_frozen(self):
        for cls in [RunSubmittedV1, RunStartedV1, RunCompletedV1,
                     RunFailedV1, RunCancelledV1, RunBlockedV1, RunGaveUpV1]:
            obj = completed("r1") if cls is RunCompletedV1 else cls(run_id=RunId("r1"))
            with pytest.raises(Exception):
                obj.run_id = RunId("r2")  # type: ignore

    def test_terminal_status_enum(self):
        assert RunTerminalStatus.COMPLETED == "completed"
        assert RunTerminalStatus.CANCELLED == "cancelled"

    def test_completed_validates_non_negative(self):
        with pytest.raises(ValueError):
            RunCompletedV1(run_id=RunId("r1"), steps_taken=-1)

    def test_factory_helpers(self):
        s = submitted("r1", turn_index=3)
        assert isinstance(s, RunSubmittedV1)
        assert str(s.run_id) == "r1"
        assert s.turn_index == 3

        c = cancelled("r2", reason="user requested")
        assert isinstance(c, RunCancelledV1)
        assert c.reason == "user requested"


# ── Envelope ────────────────────────────────────────────────────────────────

class TestEnvelope:

    @staticmethod
    def _event_type_for(payload) -> str:
        """Map payload class to its registered event type name."""
        from application.events.run_facts import (
            RunSubmittedV1, RunStartedV1, RunCompletedV1, RunFailedV1,
            RunCancelledV1, RunBlockedV1, RunGaveUpV1,
        )
        _map = {
            RunSubmittedV1: "run.submitted.v1",
            RunStartedV1: "run.started.v1",
            RunCompletedV1: "run.completed.v1",
            RunFailedV1: "run.failed.v1",
            RunCancelledV1: "run.cancelled.v1",
            RunBlockedV1: "run.blocked.v1",
            RunGaveUpV1: "run.gave_up.v1",
        }
        return _map.get(type(payload), "run.submitted.v1")

    def _envelope(self, payload):
        sid = SessionId("s1")
        return EventEnvelope(
            event_id=EventId.generate(),
            event_type=EventTypeName(self._event_type_for(payload)),
            schema_version=SchemaVersion(1),
            occurred_at=datetime.now(timezone.utc),
            source=EventSource(process_id="test", component="runtime"),
            scope=ScopeToken.session_scope(uuid.uuid4(), sid),
            correlation_id=CorrelationId("corr-1"),
            causation_id=None,
            aggregate_id=AggregateId("r1"),
            aggregate_version=AggregateVersion(1),
            payload=payload,
        )

    def test_canonical_json_roundtrip(self):
        env = self._envelope(submitted("r1"))
        js = env.canonical_json()
        data = json.loads(js)
        assert data["event_type"] == "run.submitted.v1"
        assert data["schema_version"] == 1
        assert data["payload"]["turn_index"] == 0

    def test_canonical_json_preserves_nested(self):
        env = self._envelope(completed("r1", steps_taken=5, tokens_used=1000, summary="done"))
        js = env.canonical_json()
        data = json.loads(js)
        assert data["payload"]["steps_taken"] == 5
        assert data["payload"]["tokens_used"] == 1000

    def test_event_type_name_rejects_bare(self):
        with pytest.raises(ValueError):
            EventTypeName("bare_name")

    def test_event_type_name_requires_version(self):
        EventTypeName("run.completed.v1")  # ok
        with pytest.raises(ValueError):
            EventTypeName("run.completed")  # missing vN

    def test_schema_version_minimum(self):
        with pytest.raises(ValueError):
            SchemaVersion(0)

    def test_envelope_is_frozen(self):
        env = self._envelope(submitted("r1"))
        with pytest.raises(Exception):
            env.event_type = EventTypeName("other.v1")  # type: ignore

    def test_typed_round_trip_preserves_payload_class(self):
        """R0: decode(encode(envelope)) must preserve the exact payload type.

        MUST FAIL on current code: SchemaRegistry has no decode method.
        """
        from application.events.schema_registry import SchemaRegistry
        from application.events.run_facts import RunCompletedV1

        registry = SchemaRegistry()
        env = self._envelope(completed("r1", steps_taken=5, tokens_used=100))

        js = env.canonical_json()
        # R0: registry must be able to decode JSON back to typed EventEnvelope
        decoded = registry.decode(js)
        assert decoded.event_id == env.event_id
        assert decoded.event_type == env.event_type
        assert type(decoded.payload) is RunCompletedV1, (
            f"R0 FAIL: payload type lost. Expected RunCompletedV1, got {type(decoded.payload).__name__}"
        )
