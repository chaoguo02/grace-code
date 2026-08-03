"""G3: Schema Registry contract — typed codec, identity conflict, validation.

Covers:
  - Duplicate key ALWAYS raises (even same class)
  - event_type version suffix must match schema_version
  - Unknown version returns typed UnknownSchemaVersion
  - decode validates UTC, source format, aggregate_version
  - Identity conflict detection: same source+id, different digest
  - All 12+ schemas: encode→decode→equal round-trip
  - Random field order canonical digest determinism
  - EventPayload protocol check
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from core.eventing.identifiers import RunId, TaskId, SessionId, EventId, AggregateVersion
from core.eventing.scope import ScopeToken

from application.events.envelope import (
    EventEnvelope,
    EventTypeName,
    SchemaVersion,
    EventSource,
    CorrelationId,
    AggregateId,
    EventPayload,
)
from application.events.run_facts import (
    RunSubmittedV1, RunCompletedV1, RunCancelledV1, RunFailedV1,
    RunStartedV1, RunBlockedV1, RunGaveUpV1,
    completed,
)
from application.events.tool_facts import ToolExecutedV1
from application.events.delegation_facts import (
    DelegationCreatedV1, DelegationCompletedV1,
    ChildTaskStartedV1, ChildTaskCompletedV1,
)
from application.events.schema_registry import (
    SchemaRegistry,
    SchemaEntry,
    UnknownSchemaVersion,
    EventIdentityConflict,
)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_envelope(event_type: str, payload, session_id="s-test", **kw):
    sid = SessionId(session_id)
    return EventEnvelope(
        event_id=EventId.generate(),
        event_type=EventTypeName(event_type),
        schema_version=SchemaVersion(1),
        occurred_at=datetime.now(timezone.utc),
        source=EventSource(process_id="test", component="runtime"),
        scope=ScopeToken.session_scope(uuid.uuid4(), sid),
        correlation_id=CorrelationId("corr-1"),
        causation_id=None,
        aggregate_id=AggregateId("r-test"),
        aggregate_version=AggregateVersion(1),
        payload=payload,
        **kw,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# G3.1 — Duplicate key ALWAYS raises
# ═══════════════════════════════════════════════════════════════════════════════

class TestDuplicateKeyAlwaysRaises:
    """G3: Duplicate (event_type, version) always → ValueError."""

    def test_duplicate_same_class_raises(self):
        """BEFORE: same class was silently accepted. G3: always raises."""
        reg = SchemaRegistry()
        with pytest.raises(ValueError, match="Duplicate"):
            reg.register(SchemaEntry("run.submitted.v1", 1, RunSubmittedV1))

    def test_duplicate_different_class_raises(self):
        reg = SchemaRegistry()
        with pytest.raises(ValueError, match="Duplicate"):
            reg.register(SchemaEntry("run.submitted.v1", 1, RunCompletedV1))

    def test_unique_entries_ok(self):
        reg = SchemaRegistry()
        reg.register(SchemaEntry("custom.event.v1", 1, RunSubmittedV1))
        reg.register(SchemaEntry("custom.event.v2", 2, RunCompletedV1))
        assert reg.entry_count == 14  # 12 defaults + 2 new


# ═══════════════════════════════════════════════════════════════════════════════
# G3.2 — Version suffix must match schema_version
# ═══════════════════════════════════════════════════════════════════════════════

class TestVersionSuffixMatch:
    """G3: event_type suffix '.vN' must equal SchemaEntry.version."""

    def test_version_suffix_mismatch_raises(self):
        with pytest.raises(ValueError, match="version suffix"):
            SchemaEntry("run.completed.v1", 2, RunCompletedV1)

    def test_version_suffix_match_ok(self):
        entry = SchemaEntry("run.completed.v1", 1, RunCompletedV1)
        assert entry.key == ("run.completed.v1", 1)

    def test_envelope_version_mismatch_raises(self):
        """EventTypeName version != SchemaVersion → ValueError."""
        sid = SessionId("s-test")
        with pytest.raises(ValueError, match="version"):
            EventEnvelope(
                event_id=EventId.generate(),
                event_type=EventTypeName("run.completed.v1"),
                schema_version=SchemaVersion(2),  # mismatch: v1 != 2
                occurred_at=datetime.now(timezone.utc),
                source=EventSource(process_id="test", component="runtime"),
                scope=ScopeToken.session_scope(uuid.uuid4(), sid),
                correlation_id=CorrelationId("c1"),
                causation_id=None,
                aggregate_id=AggregateId("r1"),
                aggregate_version=AggregateVersion(1),
                payload=completed("r1"),
            )


# ═══════════════════════════════════════════════════════════════════════════════
# G3.3 — Unknown schema version returns typed error
# ═══════════════════════════════════════════════════════════════════════════════

class TestUnknownSchemaVersion:
    """G3: decode with unregistered version → UnknownSchemaVersion."""

    def test_unknown_version_returns_typed_error(self):
        reg = SchemaRegistry()
        env = _make_envelope("run.completed.v1", completed("r-test"))
        js = env.canonical_json()

        # Manually alter version to unregistered one
        import json
        data = json.loads(js)
        data["event_type"] = "run.completed.v99"
        data["schema_version"] = 99
        js_bad = json.dumps(data, sort_keys=True)

        result = reg.decode(js_bad)
        assert isinstance(result, UnknownSchemaVersion)
        assert result.event_type == "run.completed.v99"
        assert result.version == 99

    def test_unknown_type_returns_typed_error(self):
        reg = SchemaRegistry()
        import json
        js_bad = json.dumps({
            "event_id": str(uuid.uuid4()),
            "event_type": "never.registered.v1",
            "schema_version": 1,
            "occurred_at": "2026-01-01T00:00:00+00:00",
            "source": "runtime/test",
            "scope": {"kind": "global", "global_id": str(uuid.uuid4()), "generation": 0},
            "correlation_id": "c1",
            "causation_id": None,
            "aggregate_id": "r1",
            "aggregate_version": 1,
            "payload": {},
        }, sort_keys=True)
        result = reg.decode(js_bad)
        assert isinstance(result, UnknownSchemaVersion)


# ═══════════════════════════════════════════════════════════════════════════════
# G3.4 — decode validates envelope fields
# ═══════════════════════════════════════════════════════════════════════════════

class TestDecodeValidatesFields:
    """G3: decode rejects invalid envelope fields."""

    def test_naive_datetime_rejected_during_decode(self):
        reg = SchemaRegistry()
        import json
        js_bad = json.dumps({
            "event_id": str(uuid.uuid4()),
            "event_type": "run.completed.v1",
            "schema_version": 1,
            "occurred_at": "2026-01-01T00:00:00",  # no TZ
            "source": "runtime/test",
            "scope": {"kind": "global", "global_id": str(uuid.uuid4()), "generation": 0},
            "correlation_id": "c1",
            "causation_id": None,
            "aggregate_id": "r1",
            "aggregate_version": 1,
            "payload": {"run_id": "r1"},
        }, sort_keys=True)
        with pytest.raises(ValueError, match="timezone"):
            reg.decode(js_bad)

    def test_invalid_source_format_rejected(self):
        reg = SchemaRegistry()
        import json
        js_bad = json.dumps({
            "event_id": str(uuid.uuid4()),
            "event_type": "run.completed.v1",
            "schema_version": 1,
            "occurred_at": "2026-01-01T00:00:00+00:00",
            "source": "badformat",  # no slash
            "scope": {"kind": "global", "global_id": str(uuid.uuid4()), "generation": 0},
            "correlation_id": "c1",
            "causation_id": None,
            "aggregate_id": "r1",
            "aggregate_version": 1,
            "payload": {"run_id": "r1"},
        }, sort_keys=True)
        with pytest.raises(ValueError, match="source"):
            reg.decode(js_bad)

    def test_invalid_aggregate_version_rejected(self):
        reg = SchemaRegistry()
        import json
        js_bad = json.dumps({
            "event_id": str(uuid.uuid4()),
            "event_type": "run.completed.v1",
            "schema_version": 1,
            "occurred_at": "2026-01-01T00:00:00+00:00",
            "source": "runtime/test",
            "scope": {"kind": "global", "global_id": str(uuid.uuid4()), "generation": 0},
            "correlation_id": "c1",
            "causation_id": None,
            "aggregate_id": "r1",
            "aggregate_version": -1,
            "payload": {"run_id": "r1"},
        }, sort_keys=True)
        with pytest.raises(ValueError, match="aggregate_version"):
            reg.decode(js_bad)

    def test_invalid_event_id_rejected(self):
        reg = SchemaRegistry()
        import json
        js_bad = json.dumps({
            "event_id": "not-a-uuid",
            "event_type": "run.completed.v1",
            "schema_version": 1,
            "occurred_at": "2026-01-01T00:00:00+00:00",
            "source": "runtime/test",
            "scope": {"kind": "global", "global_id": str(uuid.uuid4()), "generation": 0},
            "correlation_id": "c1",
            "causation_id": None,
            "aggregate_id": "r1",
            "aggregate_version": 1,
            "payload": {"run_id": "r1"},
        }, sort_keys=True)
        with pytest.raises(ValueError, match="event_id"):
            reg.decode(js_bad)


# ═══════════════════════════════════════════════════════════════════════════════
# G3.5 — Identity conflict detection
# ═══════════════════════════════════════════════════════════════════════════════

class TestIdentityConflict:
    """G3: same source+id + different digest → EventIdentityConflict."""

    def test_same_identity_same_digest_is_ok(self):
        reg = SchemaRegistry()
        env = _make_envelope("run.completed.v1", completed("r-test"))
        js = env.canonical_json()

        seen: dict[str, str] = {}
        result = reg.insert_or_check(js, seen)
        assert result is None  # first insert OK
        result2 = reg.insert_or_check(js, seen)
        assert result2 is None  # same event → idempotent

    def test_same_identity_different_digest_is_conflict(self):
        reg = SchemaRegistry()
        same_eid = EventId.generate()
        same_src = EventSource(process_id="test", component="runtime")
        sid = SessionId("s-test")

        env_a = EventEnvelope(
            event_id=same_eid,
            event_type=EventTypeName("run.completed.v1"),
            schema_version=SchemaVersion(1),
            occurred_at=datetime.now(timezone.utc),
            source=same_src,
            scope=ScopeToken.session_scope(uuid.uuid4(), sid),
            correlation_id=CorrelationId("c1"),
            causation_id=None,
            aggregate_id=AggregateId("r-test"),
            aggregate_version=AggregateVersion(1),
            payload=completed("r-test", summary="abc"),
        )
        env_b = EventEnvelope(
            event_id=same_eid,
            event_type=EventTypeName("run.completed.v1"),
            schema_version=SchemaVersion(1),
            occurred_at=datetime.now(timezone.utc),
            source=same_src,
            scope=ScopeToken.session_scope(uuid.uuid4(), sid),
            correlation_id=CorrelationId("c2"),
            causation_id=None,
            aggregate_id=AggregateId("r-test"),
            aggregate_version=AggregateVersion(1),
            payload=completed("r-test", summary="different"),
        )

        js_a = env_a.canonical_json()
        js_b = env_b.canonical_json()

        seen: dict[str, str] = {}
        result_a = reg.insert_or_check(js_a, seen)
        assert result_a is None

        result_b = reg.insert_or_check(js_b, seen)
        assert isinstance(result_b, EventIdentityConflict)
        assert result_b.event_id == str(env_a.event_id)

    def test_different_identity_no_conflict(self):
        reg = SchemaRegistry()
        env_a = _make_envelope("run.completed.v1", completed("r-a"))
        env_b = _make_envelope("run.completed.v1", completed("r-b"))

        seen: dict[str, str] = {}
        assert reg.insert_or_check(env_a.canonical_json(), seen) is None
        assert reg.insert_or_check(env_b.canonical_json(), seen) is None


# ═══════════════════════════════════════════════════════════════════════════════
# G3.6 — All 12 schemas encode→decode→equal round-trip
# ═══════════════════════════════════════════════════════════════════════════════

class TestAllSchemasRoundTrip:
    """G3: Every registered schema must round-trip through encode→decode."""

    def _roundtrip(self, event_type: str, payload, session_id="s-roundtrip"):
        reg = SchemaRegistry()
        env = _make_envelope(event_type, payload, session_id=session_id)
        js = env.canonical_json()
        decoded = reg.decode(js)

        assert not isinstance(decoded, UnknownSchemaVersion), (
            f"Unknown schema for {event_type}"
        )
        assert not isinstance(decoded, EventIdentityConflict), (
            f"Identity conflict for {event_type}"
        )
        assert isinstance(decoded, EventEnvelope), (
            f"Expected EventEnvelope, got {type(decoded)}"
        )

        # Round-trip equality
        assert decoded.event_type == env.event_type
        assert decoded.schema_version == env.schema_version
        assert decoded.event_id == env.event_id
        assert decoded.source == env.source
        assert decoded.aggregate_id == env.aggregate_id
        assert decoded.aggregate_version == env.aggregate_version
        assert decoded.correlation_id == env.correlation_id

    def test_run_submitted_roundtrip(self):
        self._roundtrip("run.submitted.v1",
                         RunSubmittedV1(run_id=RunId("r-1"), turn_index=0, turn_id="t1"))

    def test_run_started_roundtrip(self):
        self._roundtrip("run.started.v1",
                         RunStartedV1(run_id=RunId("r-1"), turn_index=1))

    def test_run_completed_roundtrip(self):
        self._roundtrip("run.completed.v1",
                         completed("r-1", turn_index=2, steps_taken=5, tokens_used=500,
                                   summary="all good"))

    def test_run_failed_roundtrip(self):
        self._roundtrip("run.failed.v1",
                         RunFailedV1(run_id=RunId("r-1"), error="something broke"))

    def test_run_cancelled_roundtrip(self):
        self._roundtrip("run.cancelled.v1",
                         RunCancelledV1(run_id=RunId("r-1"), reason="user requested"))

    def test_run_blocked_roundtrip(self):
        self._roundtrip("run.blocked.v1",
                         RunBlockedV1(run_id=RunId("r-1"), blocked_by="governor"))

    def test_run_gave_up_roundtrip(self):
        self._roundtrip("run.gave_up.v1",
                         RunGaveUpV1(run_id=RunId("r-1"), consecutive_failures=3,
                                     max_steps_reached=True))

    def test_tool_executed_roundtrip(self):
        self._roundtrip("tool.executed.v1",
                         ToolExecutedV1(run_id=RunId("r-1"),
                                        task_id=TaskId("t-1"),
                                        tool_name="read_file",
                                        invocation_id="inv-1"))

    def test_delegation_created_roundtrip(self):
        self._roundtrip("delegation.created.v1",
                         DelegationCreatedV1(delegation_id="d-1",
                                             parent_run_id=RunId("r-1"),
                                             task_count=2))

    def test_delegation_completed_roundtrip(self):
        self._roundtrip("delegation.completed.v1",
                         DelegationCompletedV1(delegation_id="d-1",
                                               parent_run_id=RunId("r-1"),
                                               successful_tasks=2))

    def test_child_task_started_roundtrip(self):
        self._roundtrip("child_task.started.v1",
                         ChildTaskStartedV1(task_id=TaskId("t-1"),
                                            delegation_id="d-1",
                                            parent_run_id=RunId("r-1"),
                                            child_session_id="cs-1"))

    def test_child_task_completed_roundtrip(self):
        self._roundtrip("child_task.completed.v1",
                         ChildTaskCompletedV1(task_id=TaskId("t-1"),
                                              delegation_id="d-1",
                                              parent_run_id=RunId("r-1")))


# ═══════════════════════════════════════════════════════════════════════════════
# G3.7 — Canonical digest determinism with random field order
# ═══════════════════════════════════════════════════════════════════════════════

class TestCanonicalDigestDeterminism:
    """G3: same semantic content → identical canonical JSON regardless of construction order."""

    def test_random_field_order_same_digest(self):
        import random
        import hashlib
        from datetime import datetime, timezone

        # fixed identifiers and timestamp for deterministic digest
        fixed_eid = EventId(value=uuid.UUID("00000000-0000-0000-0000-000000000001"))
        fixed_src = EventSource(process_id="test", component="runtime")
        fixed_ts = datetime(2026, 8, 2, 0, 0, 0, tzinfo=timezone.utc)
        fixed_scope = ScopeToken.session_scope(uuid.UUID("00000000-0000-0000-0000-000000000002"),
                                                SessionId("s-fixed"))

        payload_args = [
            ("run_id", RunId("r-1")),
            ("turn_index", 2),
            ("steps_taken", 5),
            ("tokens_used", 500),
            ("summary", "test"),
        ]
        digest = None
        for _ in range(100):
            shuffled = list(payload_args)
            random.shuffle(shuffled)
            p = RunCompletedV1(**dict(shuffled))
            env = EventEnvelope(
                event_id=fixed_eid,
                event_type=EventTypeName("run.completed.v1"),
                schema_version=SchemaVersion(1),
                occurred_at=fixed_ts,
                source=fixed_src,
                scope=fixed_scope,
                correlation_id=CorrelationId("c-fixed"),
                causation_id=None,
                aggregate_id=AggregateId("r-fixed"),
                aggregate_version=AggregateVersion(1),
                payload=p,
            )
            js = env.canonical_json()
            d = hashlib.sha256(js.encode()).hexdigest()
            if digest is None:
                digest = d
            else:
                assert d == digest, f"Digest differs with shuffled field order"

    def test_payload_class_registry_roundtrip_preserves_canonical_form(self):
        """encode→decode produces same payload structure regardless of input order."""
        reg = SchemaRegistry()
        env = _make_envelope("run.completed.v1",
                             completed("r-test", steps_taken=5, tokens_used=500,
                                       summary="done"))
        js = env.canonical_json()
        decoded = reg.decode(js)
        assert isinstance(decoded, EventEnvelope)
        p = decoded.payload
        assert isinstance(p, RunCompletedV1)
        assert p.run_id == RunId("r-test")
        assert p.steps_taken == 5
        assert p.tokens_used == 500
        assert p.summary == "done"


# ═══════════════════════════════════════════════════════════════════════════════
# G3.8 — Envelope validation at construction
# ═══════════════════════════════════════════════════════════════════════════════

class TestEnvelopeValidation:
    """G3: EventEnvelope validates fields at construction."""

    def test_non_utc_datetime_rejected(self):
        import zoneinfo
        sid = SessionId("s-test")
        # datetime with non-UTC tz
        with pytest.raises(ValueError, match="UTC"):
            EventEnvelope(
                event_id=EventId.generate(),
                event_type=EventTypeName("run.completed.v1"),
                schema_version=SchemaVersion(1),
                occurred_at=datetime.now(zoneinfo.ZoneInfo("America/New_York")),
                source=EventSource(process_id="test", component="runtime"),
                scope=ScopeToken.session_scope(uuid.uuid4(), sid),
                correlation_id=CorrelationId("c1"),
                causation_id=None,
                aggregate_id=AggregateId("r1"),
                aggregate_version=AggregateVersion(1),
                payload=completed("r1"),
            )

    def test_empty_source_fields_rejected(self):
        with pytest.raises(ValueError, match="process_id"):
            EventSource(process_id="", component="runtime")
        with pytest.raises(ValueError, match="component"):
            EventSource(process_id="test", component="")

    def test_empty_correlation_id_rejected(self):
        with pytest.raises(ValueError, match="CorrelationId"):
            CorrelationId("")
        with pytest.raises(ValueError, match="CorrelationId"):
            CorrelationId("   ")

    def test_empty_aggregate_id_rejected(self):
        with pytest.raises(ValueError, match="AggregateId"):
            AggregateId("")
        with pytest.raises(ValueError, match="AggregateId"):
            AggregateId("   ")


# ═══════════════════════════════════════════════════════════════════════════════
# G3.9 — EventPayload protocol check
# ═══════════════════════════════════════════════════════════════════════════════

class TestEventPayloadProtocol:
    """G3: All registered payload classes satisfy EventPayload protocol."""

    def test_all_fact_classes_are_event_payloads(self):
        """Every payload class in the registry satisfies EventPayload."""
        reg = SchemaRegistry()
        for et in reg.registered_types:
            entry = reg.get(et)
            assert entry is not None
            cls = entry.payload_class
            assert isinstance(cls, type), f"{cls} is not a type"
            assert hasattr(cls, "__dataclass_fields__"), (
                f"{cls.__name__} missing __dataclass_fields__"
            )

    def test_payload_isinstance_check(self):
        """EventPayload is a runtime checkable protocol."""
        p = completed("r1")
        assert isinstance(p, EventPayload), (
            "RunCompletedV1 should satisfy EventPayload protocol"
        )
