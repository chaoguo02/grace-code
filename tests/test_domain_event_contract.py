"""R3.0 Task A: DomainEvent v2 contract — acceptance tests.

AC-R3-01: All DomainEvent are frozen dataclass.
AC-R3-02: JSON round-trip preserves content equality.
AC-R3-03: event_type is stable protocol name (not Python class name).
AC-R3-04: Payload is JSON-safe (no Any, no exception, no callback).
"""

from __future__ import annotations

import json

import pytest

from server.domain_events import (
    DomainEvent,
    session_started, session_completed, session_cancelled, session_failed,
    run_submitted, run_started, run_completed, run_cancelled, run_failed,
    tool_executed,
)


class TestDomainEventEnvelope:

    def test_frozen_dataclass(self):
        """AC-R3-01: DomainEvent is frozen — immutable after creation."""
        ev = session_started(session_id="s1")
        with pytest.raises(Exception):
            ev.event_type = "modified"  # type: ignore

    def test_event_type_is_protocol_name(self):
        """AC-R3-03: event_type is a stable protocol string."""
        ev = session_started(session_id="s1")
        assert ev.event_type == "session.started"
        assert not ev.event_type.startswith("DomainEvent")

    def test_payload_is_json_safe(self):
        """AC-R3-04: payload contains only JSON-safe types."""
        ev = run_completed(
            session_id="s1", run_id="r1", aggregate_version=1,
            steps_taken=5, tokens_used=1000,
        )
        payload = ev.payload
        assert isinstance(payload["steps_taken"], int)
        assert isinstance(payload["tokens_used"], int)


class TestJsonRoundTrip:

    def test_round_trip_preserves_content(self):
        """AC-R3-02: serialize → deserialize returns equal content."""
        ev = run_completed(
            session_id="s1", run_id="r1", aggregate_version=3,
            steps_taken=10, tokens_used=5000,
        )
        restored = DomainEvent.from_json(ev.json())
        assert restored.event_id == ev.event_id
        assert restored.event_type == ev.event_type
        assert restored.session_id == ev.session_id
        assert restored.aggregate_id == ev.aggregate_id
        assert restored.aggregate_version == ev.aggregate_version
        assert restored.payload == ev.payload

    def test_all_concrete_events_round_trip(self):
        """Every factory function produces round-trip-safe events."""
        events = [
            session_started("s"),
            session_completed("s", 1),
            session_cancelled("s", 1, "user requested"),
            session_failed("s", 1, "crash"),
            run_submitted("s", "r"),
            run_started("s", "r", 1),
            run_completed("s", "r", 2, 10, 5000),
            run_cancelled("s", "r", 2, "timeout"),
            run_failed("s", "r", 2, "OOM"),
            tool_executed("s", "r", 3, "Bash", "inv1", True, 150.0),
        ]
        for ev in events:
            restored = DomainEvent.from_json(ev.json())
            assert restored.event_id == ev.event_id, f"Failed for {ev.event_type}"
            assert restored.payload == ev.payload, f"Failed for {ev.event_type}"

    def test_unknown_version_survives_round_trip(self):
        """Unknown event_version should not cause data loss."""
        ev = DomainEvent(
            event_type="unknown.future",
            event_version=99,
            session_id="s",
            aggregate_id="a",
            payload={"future_field": "value"},
        )
        restored = DomainEvent.from_json(ev.json())
        assert restored.event_version == 99
        assert restored.payload["future_field"] == "value"
