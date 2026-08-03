"""
P14: Run commands — frozen command objects for the Coordinator.

Commands are intent, not events.  They go to the Coordinator directly,
never through the EventBus.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.eventing.identifiers import SessionId, RunId, AggregateVersion
from runtime_core.outcome import RuntimeOutcome, CancellationReason


@dataclass(frozen=True, slots=True)
class SubmitRun:
    session_id: SessionId
    prompt: str
    idempotency_key: str = ""


@dataclass(frozen=True, slots=True)
class ExecuteRun:
    session_id: SessionId
    run_id: RunId


@dataclass(frozen=True, slots=True)
class CancelRun:
    run_id: RunId
    reason: CancellationReason = CancellationReason.USER_REQUESTED


@dataclass(frozen=True, slots=True)
class FinalizeRun:
    run_id: RunId
    expected_version: AggregateVersion
    outcome: RuntimeOutcome
