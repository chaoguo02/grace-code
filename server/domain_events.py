"""
G38: DEPRECATED — replaced by application.events.envelope.EventEnvelope + FrozenJsonObject.

Old DomainEvent with `payload: dict` is kept for backward compat only.
New code MUST use EventEnvelope[RunCompletedV1] etc. from application.events.
asdict() is replaced by explicit codec in application.events.envelope.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

# ── DomainEvent ─────────────────────────────────────────────────────────────

@dataclass(frozen=True, kw_only=True)
class DomainEvent:
    """Stable protocol envelope for all durable domain facts.

    Fields:
        event_id:          Unique; generated once; retries MUST reuse.
        event_type:        Stable protocol name (NOT Python class name).
        event_version:     Schema version of this payload.
        session_id:        Scope identifier — the only scope source.
        aggregate_id:      Entity this event belongs to (e.g. run_id).
        aggregate_version: Monotonic counter within the aggregate.
        occurred_at:       Wall-clock time the fact happened.
        correlation_id:    Links events across aggregates (optional).
        causation_id:      Immediate parent event_id (optional).
        payload:           JSON-safe dict with the fact's data.
    """
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str
    event_version: int = 1
    session_id: str
    aggregate_id: str
    aggregate_version: int = 1
    occurred_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    correlation_id: str = ""
    causation_id: str = ""
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> DomainEvent:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, s: str) -> DomainEvent:
        return cls.from_dict(json.loads(s))


# ── Concrete event types (payload helpers) ──────────────────────────────────

def session_started(
    session_id: str,
    aggregate_version: int = 1,
) -> DomainEvent:
    return DomainEvent(
        event_type="session.started",
        session_id=session_id,
        aggregate_id=session_id,
        aggregate_version=aggregate_version,
    )


def session_completed(
    session_id: str,
    aggregate_version: int,
    steps_taken: int = 0,
) -> DomainEvent:
    return DomainEvent(
        event_type="session.completed",
        session_id=session_id,
        aggregate_id=session_id,
        aggregate_version=aggregate_version,
        payload={"steps_taken": steps_taken},
    )


def session_cancelled(
    session_id: str,
    aggregate_version: int,
    reason: str = "",
) -> DomainEvent:
    return DomainEvent(
        event_type="session.cancelled",
        session_id=session_id,
        aggregate_id=session_id,
        aggregate_version=aggregate_version,
        payload={"reason": reason},
    )


def session_failed(
    session_id: str,
    aggregate_version: int,
    error: str = "",
) -> DomainEvent:
    return DomainEvent(
        event_type="session.failed",
        session_id=session_id,
        aggregate_id=session_id,
        aggregate_version=aggregate_version,
        payload={"error": error},
    )


def run_submitted(
    session_id: str,
    run_id: str,
    aggregate_version: int = 1,
) -> DomainEvent:
    return DomainEvent(
        event_type="run.submitted",
        session_id=session_id,
        aggregate_id=run_id,
        aggregate_version=aggregate_version,
    )


def run_started(
    session_id: str,
    run_id: str,
    aggregate_version: int,
) -> DomainEvent:
    return DomainEvent(
        event_type="run.started",
        session_id=session_id,
        aggregate_id=run_id,
        aggregate_version=aggregate_version,
    )


def run_completed(
    session_id: str,
    run_id: str,
    aggregate_version: int,
    steps_taken: int = 0,
    tokens_used: int = 0,
) -> DomainEvent:
    return DomainEvent(
        event_type="run.completed",
        session_id=session_id,
        aggregate_id=run_id,
        aggregate_version=aggregate_version,
        payload={"steps_taken": steps_taken, "tokens_used": tokens_used},
    )


def run_cancelled(
    session_id: str,
    run_id: str,
    aggregate_version: int,
    reason: str = "",
) -> DomainEvent:
    return DomainEvent(
        event_type="run.cancelled",
        session_id=session_id,
        aggregate_id=run_id,
        aggregate_version=aggregate_version,
        payload={"reason": reason},
    )


def run_failed(
    session_id: str,
    run_id: str,
    aggregate_version: int,
    error: str = "",
) -> DomainEvent:
    return DomainEvent(
        event_type="run.failed",
        session_id=session_id,
        aggregate_id=run_id,
        aggregate_version=aggregate_version,
        payload={"error": error},
    )


def tool_executed(
    session_id: str,
    run_id: str,
    aggregate_version: int,
    tool_name: str = "",
    invocation_id: str = "",
    success: bool = True,
    duration_ms: float = 0.0,
) -> DomainEvent:
    return DomainEvent(
        event_type="tool.executed",
        session_id=session_id,
        aggregate_id=run_id,
        aggregate_version=aggregate_version,
        payload={
            "tool_name": tool_name,
            "invocation_id": invocation_id,
            "success": success,
            "duration_ms": duration_ms,
        },
    )
