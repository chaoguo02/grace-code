"""
P2: EventEnvelope — generic typed carrier, NOT GenericEvent/dict.

Cannot be instantiated as EventEnvelope[dict].  Payload must be a
registered dataclass from the schema registry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Generic, TypeVar

from core.eventing.identifiers import EventId, AggregateVersion
from core.eventing.scope import ScopeToken

PayloadT = TypeVar("PayloadT", covariant=True)


# ── Supporting types ────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class EventTypeName:
    value: str

    def __post_init__(self) -> None:
        if "." not in self.value:
            raise ValueError(f"EventTypeName must be 'domain.event.vN', got {self.value}")
        parts = self.value.rsplit(".", 1)
        if not parts[-1].startswith("v"):
            raise ValueError(f"EventTypeName must end with vN version, got {self.value}")

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

    def __str__(self) -> str:
        return f"{self.component}/{self.process_id}"


@dataclass(frozen=True, slots=True)
class CorrelationId:
    value: str

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class AggregateId:
    value: str

    def __str__(self) -> str:
        return self.value


# ── EventEnvelope ───────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class EventEnvelope(Generic[PayloadT]):
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
    payload: PayloadT

    def canonical_json(self) -> str:
        """Canonical JSON — not default asdict. Preserves type semantics."""
        d = {
            "event_id": str(self.event_id),
            "event_type": str(self.event_type),
            "schema_version": self.schema_version.value,
            "occurred_at": self.occurred_at.isoformat(),
            "source": str(self.source),
            "scope": _scope_to_dict(self.scope),
            "correlation_id": str(self.correlation_id),
            "causation_id": str(self.causation_id) if self.causation_id else None,
            "aggregate_id": str(self.aggregate_id),
            "aggregate_version": self.aggregate_version.value,
            "payload": _payload_to_dict(self.payload),
        }
        return json.dumps(d, ensure_ascii=False, sort_keys=True)


def _scope_to_dict(scope: ScopeToken) -> dict:
    d: dict = {
        "kind": scope.kind.value,
        "global_id": str(scope.global_id),
        "generation": scope.generation,
    }
    if scope.session_id is not None:
        d["session_id"] = str(scope.session_id)
    if scope.task_id is not None:
        d["task_id"] = str(scope.task_id)
    return d


def _payload_to_dict(payload) -> dict:
    """Convert payload dataclass to dict.  Recursively handles nested dataclasses."""
    if hasattr(payload, "__dataclass_fields__"):
        return {k: _payload_to_dict(v) for k, v in asdict(payload).items()}
    if isinstance(payload, (list, tuple)):
        return [_payload_to_dict(v) for v in payload]
    return payload
