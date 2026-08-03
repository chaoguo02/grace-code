"""
G22: Run commands — frozen, typed, with idempotency/conflict types.

Commands are intent, not events.  Direct to Coordinator, never EventBus.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.eventing.identifiers import SessionId, RunId, AggregateVersion
from runtime_core.outcome import RuntimeOutcome, CancellationReason


# ── Commands ────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SubmitRun:
    session_id: SessionId
    prompt: str
    idempotency_key: str = ""
    expected_version: int = 0  # G22: CAS for active run check


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


# ── G22: Typed errors ───────────────────────────────────────────────────────

class IdempotencyConflict(RuntimeError):
    """Same idempotency_key, different payload — permanent conflict."""
    def __init__(self, key: str = "", existing_run_id: str = "",
                 message: str = "") -> None:
        super().__init__(message)
        self.key = key
        self.existing_run_id = existing_run_id


class RunAlreadyActive(RuntimeError):
    """A run is already active for this session."""
    def __init__(self, session_id: str = "", active_run_id: str = "",
                 message: str = "") -> None:
        super().__init__(message)
        self.session_id = session_id
        self.active_run_id = active_run_id


# ── Submit result ───────────────────────────────────────────────────────────

SubmitResult = RunId | IdempotencyConflict | RunAlreadyActive
"""Result of submitting a run: the RunId on success, or a typed error."""
