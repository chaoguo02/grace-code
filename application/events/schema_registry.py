"""
P3: Schema Registry — whitelist of payload classes by (event_type, version).

Duplicate registration = ValueError at startup.
Only registered payload classes can be wrapped in EventEnvelope.
"""

from __future__ import annotations

import types as _types
import uuid as _uuid
from dataclasses import dataclass, fields as dc_fields
from datetime import datetime, timezone
from typing import Any

from application.events.run_facts import (
    RunSubmittedV1, RunStartedV1, RunCompletedV1, RunFailedV1,
    RunCancelledV1, RunBlockedV1, RunGaveUpV1,
)
from application.events.tool_facts import ToolExecutedV1
from application.events.delegation_facts import (
    DelegationCreatedV1, DelegationCompletedV1,
    ChildTaskStartedV1, ChildTaskCompletedV1,
)
from core.eventing.identifiers import RunId, TaskId, SessionId, EventId
from core.eventing.scope import ScopeToken, ScopeKind
from application.events.envelope import (
    EventEnvelope, EventTypeName, SchemaVersion, EventSource,
    CorrelationId, AggregateId, AggregateVersion,
)


@dataclass(frozen=True, slots=True)
class SchemaEntry:
    event_type: str       # e.g. "run.completed.v1"
    version: int
    payload_class: type
    description: str = ""


class SchemaRegistry:
    """Immutable after construction. Duplicate keys → ValueError."""

    def __init__(self) -> None:
        self._entries: dict[str, SchemaEntry] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        defaults: list[SchemaEntry] = [
            # Run facts
            SchemaEntry("run.submitted.v1", 1, RunSubmittedV1),
            SchemaEntry("run.started.v1", 1, RunStartedV1),
            SchemaEntry("run.completed.v1", 1, RunCompletedV1),
            SchemaEntry("run.failed.v1", 1, RunFailedV1),
            SchemaEntry("run.cancelled.v1", 1, RunCancelledV1),
            SchemaEntry("run.blocked.v1", 1, RunBlockedV1),
            SchemaEntry("run.gave_up.v1", 1, RunGaveUpV1),
            # Tool facts
            SchemaEntry("tool.executed.v1", 1, ToolExecutedV1),
            # Delegation facts
            SchemaEntry("delegation.created.v1", 1, DelegationCreatedV1),
            SchemaEntry("delegation.completed.v1", 1, DelegationCompletedV1),
            SchemaEntry("child_task.started.v1", 1, ChildTaskStartedV1),
            SchemaEntry("child_task.completed.v1", 1, ChildTaskCompletedV1),
        ]
        for entry in defaults:
            self.register(entry)

    def register(self, entry: SchemaEntry) -> None:
        key = entry.event_type
        if key in self._entries:
            existing = self._entries[key]
            if existing.payload_class is not entry.payload_class:
                raise ValueError(
                    f"Duplicate schema key '{key}': "
                    f"existing={existing.payload_class.__name__}, "
                    f"new={entry.payload_class.__name__}"
                )
        self._entries[key] = entry

    def get(self, event_type: str) -> SchemaEntry | None:
        return self._entries.get(event_type)

    def has(self, event_type: str) -> bool:
        return event_type in self._entries

    def validate_payload(self, event_type: str, payload: Any) -> bool:
        """True if *payload* is an instance of the registered class."""
        entry = self._entries.get(event_type)
        if entry is None:
            return False
        return isinstance(payload, entry.payload_class)

    # ── Decode ──────────────────────────────────────────────────────────

    def decode(self, json_str: str) -> EventEnvelope:
        """Decode canonical JSON back to typed EventEnvelope.

        Reconstructs the exact payload class registered for event_type.
        Supports round-trip: decode(encode(envelope)) == envelope.
        """
        import json as _json

        data = _json.loads(json_str)
        event_type = data["event_type"]

        entry = self._entries.get(event_type)
        if entry is None:
            raise ValueError(f"Unknown event type: {event_type}")

        payload = _build_payload(entry.payload_class, data.get("payload", {}))

        # Reconstruct scope
        scope_data = data["scope"]
        scope = ScopeToken(
            kind=ScopeKind(scope_data["kind"]),
            global_id=_uuid.UUID(scope_data["global_id"]),
            generation=scope_data["generation"],
            session_id=SessionId(scope_data["session_id"]) if scope_data.get("session_id") else None,
            task_id=TaskId(scope_data["task_id"]) if scope_data.get("task_id") else None,
        )

        # Reconstruct source (format: "component/process_id")
        source_str = data["source"]
        if "/" in source_str:
            component, process_id = source_str.split("/", 1)
        else:
            component, process_id = source_str, ""

        return EventEnvelope(
            event_id=EventId(value=_uuid.UUID(data["event_id"])),
            event_type=EventTypeName(event_type),
            schema_version=SchemaVersion(data["schema_version"]),
            occurred_at=datetime.fromisoformat(data["occurred_at"]),
            source=EventSource(process_id=process_id, component=component),
            scope=scope,
            correlation_id=CorrelationId(data["correlation_id"]),
            causation_id=EventId(value=_uuid.UUID(data["causation_id"])) if data.get("causation_id") else None,
            aggregate_id=AggregateId(data["aggregate_id"]),
            aggregate_version=AggregateVersion(data["aggregate_version"]),
            payload=payload,
        )

    @property
    def registered_types(self) -> list[str]:
        return sorted(self._entries.keys())


# ── Payload reconstruction helpers ────────────────────────────────────────

def _build_payload(payload_class: type, data: dict) -> Any:
    """Reconstruct a typed payload dataclass from a plain dict."""
    kwargs: dict[str, Any] = {}
    for f in dc_fields(payload_class):
        key = f.name
        if key not in data:
            continue
        kwargs[key] = _coerce_field(f.type, data[key])
    return payload_class(**kwargs)


def _coerce_field(target_type: type, value: Any) -> Any:
    """Coerce a JSON value to match the expected field type annotation."""
    if value is None:
        return None

    # Handle Optional[X] = X | None (Python 3.10+ UnionType)
    origin = getattr(target_type, '__origin__', None)
    if origin is _types.UnionType:
        args = getattr(target_type, '__args__', ())
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _coerce_field(non_none[0], value)
        return value

    # Handle nested dataclass (including RunId, TaskId) — dict values from asdict()
    if hasattr(target_type, '__dataclass_fields__') and isinstance(value, dict):
        return _build_payload(target_type, value)

    # Value objects with single-arg string constructors (plain string values)
    if target_type is RunId and isinstance(value, str):
        return RunId(value)
    if target_type is TaskId and isinstance(value, str):
        return TaskId(value)

    return value
