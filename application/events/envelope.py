"""
G3: Typed EventEnvelope — explicit codec, no asdict(), validated fields.

- EventPayload protocol for all payload classes
- Explicit field encoder/decoder per SchemaEntry (not loose coercion)
- All fields validated at construction: UTC timestamp, non-empty IDs
- canonical_json() uses FrozenJsonObject for deterministic output
"""

from __future__ import annotations

import uuid as _uuid
from dataclasses import dataclass, fields as dc_fields
from datetime import datetime, timezone
from typing import Generic, Protocol, TypeVar, runtime_checkable

from core.eventing.identifiers import EventId, AggregateVersion
from core.eventing.scope import ScopeToken
from core.json_values import FrozenJsonObject, JsonValue, freeze_json, thaw_json
from core.json_codec import canonical_dumps_string


# ── Payload protocol ─────────────────────────────────────────────────────────

@runtime_checkable
class EventPayload(Protocol):
    """Protocol for typed event payloads.

    Every payload class MUST be a frozen dataclass whose fields are
    JSON-serializable value objects (str, int, float, bool, None,
    nested frozen dataclasses, or tuples thereof).
    """

    def __dataclass_fields__(self) -> dict: ...


PayloadT_co = TypeVar("PayloadT_co", bound=EventPayload, covariant=True)


# ── Supporting types ────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class EventTypeName:
    value: str

    def __post_init__(self) -> None:
        if "." not in self.value:
            raise ValueError(
                f"EventTypeName must be 'domain.event.vN', got {self.value}"
            )
        parts = self.value.rsplit(".", 1)
        if not parts[-1].startswith("v"):
            raise ValueError(
                f"EventTypeName must end with vN version, got {self.value}"
            )
        # Parse version number
        try:
            _ver = int(parts[-1][1:])
        except ValueError:
            raise ValueError(
                f"EventTypeName version must be integer, got {parts[-1]}"
            )

    @property
    def version(self) -> int:
        """Extract the version number from the event type name."""
        return int(self.value.rsplit(".", 1)[-1][1:])

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SchemaVersion:
    value: int

    def __post_init__(self) -> None:
        if self.value < 1:
            raise ValueError(f"SchemaVersion must be >= 1, got {self.value}")

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class EventSource:
    process_id: str
    component: str  # "runtime" | "coordinator" | "hook"

    def __post_init__(self) -> None:
        if not self.process_id.strip():
            raise ValueError("EventSource.process_id must not be empty")
        if not self.component.strip():
            raise ValueError("EventSource.component must not be empty")

    def __str__(self) -> str:
        return f"{self.component}/{self.process_id}"


@dataclass(frozen=True, slots=True)
class CorrelationId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("CorrelationId must not be empty")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class AggregateId:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("AggregateId must not be empty")

    def __str__(self) -> str:
        return self.value


# ── EventEnvelope ───────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class EventEnvelope(Generic[PayloadT_co]):
    event_id: EventId
    event_type: EventTypeName
    schema_version: SchemaVersion
    occurred_at: datetime
    source: EventSource
    scope: ScopeToken
    correlation_id: CorrelationId
    causation_id: EventId | None
    aggregate_id: AggregateId
    aggregate_version: AggregateVersion
    payload: PayloadT_co

    def __post_init__(self) -> None:
        # Validate UTC
        if self.occurred_at.tzinfo is None:
            raise ValueError(
                f"occurred_at must be timezone-aware UTC, got {self.occurred_at}"
            )
        if self.occurred_at.tzinfo != timezone.utc:
            raise ValueError(
                f"occurred_at must be UTC, got {self.occurred_at.tzinfo}"
            )
        # Validate event_type version matches schema_version
        if self.event_type.version != self.schema_version.value:
            raise ValueError(
                f"Event type '{self.event_type}' has version "
                f"{self.event_type.version}, but schema_version is "
                f"{self.schema_version.value}"
            )

    def canonical_json(self) -> str:
        """Canonical JSON string — deterministic, sorted keys, compact."""
        return canonical_dumps_string(self._to_frozen())

    def _to_frozen(self) -> FrozenJsonObject:
        """Convert to FrozenJsonObject for deterministic JSON encoding."""
        scope_d: dict = {
            "kind": self.scope.kind.value,
            "global_id": str(self.scope.global_id),
            "generation": self.scope.generation,
        }
        if self.scope.session_id is not None:
            scope_d["session_id"] = str(self.scope.session_id)
        if self.scope.task_id is not None:
            scope_d["task_id"] = str(self.scope.task_id)

        payload_d = _payload_to_plain_dict(self.payload)

        return freeze_json({
            "event_id": str(self.event_id),
            "event_type": str(self.event_type),
            "schema_version": self.schema_version.value,
            "occurred_at": self.occurred_at.isoformat(),
            "source": str(self.source),
            "scope": scope_d,
            "correlation_id": str(self.correlation_id),
            "causation_id": str(self.causation_id) if self.causation_id else None,
            "aggregate_id": str(self.aggregate_id),
            "aggregate_version": self.aggregate_version.value,
            "payload": payload_d,
        })


# ── Payload encode helpers (explicit, NOT asdict) ───────────────────────────

def _payload_to_plain_dict(payload) -> dict:
    """Convert a typed payload dataclass to a plain dict for JSON encoding.

    Uses explicit field iteration — NOT asdict().  Values must be:
    - JSON scalars (str, int, float, bool, None)
    - immutable value objects with a single-arg str constructor
    - nested dataclass payloads (recursively encoded)
    - tuples of the above
    """
    result: dict = {}
    for f in dc_fields(payload):
        val = getattr(payload, f.name)
        result[f.name] = _field_to_plain(val)
    return result


def _field_to_plain(val):
    """Convert a single field value to its plain JSON representation.

    Simple value objects (single-field frozen dataclasses like RunId, TaskId)
    are serialized as their string representation.
    Nested payload dataclasses (multi-field) are serialized as objects.
    """
    if val is None:
        return None
    if isinstance(val, (bool, int, float, str)):
        return val
    if isinstance(val, (tuple, list)):
        return [_field_to_plain(v) for v in val]
    if hasattr(val, "__dataclass_fields__"):
        # Value object: single-field dataclass → serialize as str
        fields = tuple(val.__dataclass_fields__.keys())
        if len(fields) == 1:
            return str(val)
        # Nested payload dataclass → serialize as object
        return _payload_to_plain_dict(val)
    # Fallback: str() for unknown typed values
    return str(val)


# ── Payload decode helpers (explicit field reconstruction) ───────────────────

def _payload_from_plain_dict(payload_class: type, data: dict) -> EventPayload:
    """Reconstruct a typed payload from a plain dict.

    Each field is coerced from its JSON representation to the declared type.
    Uses resolved type hints (handles `from __future__ import annotations`).
    """
    import typing as _typing

    # Resolve string annotations (PEP 563: from __future__ import annotations)
    try:
        resolved_hints = _typing.get_type_hints(payload_class)
    except Exception:
        resolved_hints = {}

    kwargs: dict = {}
    for f in dc_fields(payload_class):
        key = f.name
        if key not in data:
            continue
        # Use resolved annotation if available, else fall back to f.type (may be string)
        target_type = resolved_hints.get(f.name, f.type)
        kwargs[key] = _field_from_plain(target_type, data[key])
    return payload_class(**kwargs)


def _field_from_plain(target_type: type, value):
    """Coerce a JSON value to match the expected field type annotation."""
    import types as _types

    if value is None:
        return None

    origin = getattr(target_type, "__origin__", None)

    # Handle Optional[X] = X | None
    if origin is _types.UnionType:
        args = getattr(target_type, "__args__", ())
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _field_from_plain(non_none[0], value)
        return value

    # Handle nested dataclass payloads
    if hasattr(target_type, "__dataclass_fields__") and isinstance(value, dict):
        return _payload_from_plain_dict(target_type, value)

    # Handle value objects (single-field dataclasses like RunId, TaskId)
    if hasattr(target_type, "__dataclass_fields__"):
        fields = tuple(target_type.__dataclass_fields__.keys())
        if len(fields) == 1 and isinstance(value, str):
            try:
                return target_type(value)
            except (TypeError, ValueError):
                pass
        elif len(fields) > 1 and isinstance(value, dict):
            return _payload_from_plain_dict(target_type, value)

    return value
