"""
P12: Runtime outcome — frozen result of a run execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from core.eventing.identifiers import RunId


class RunStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    GAVE_UP = "gave_up"


class CancellationReason(StrEnum):
    USER_REQUESTED = "user_requested"
    TIMEOUT = "timeout"
    HOOK_BLOCKED = "hook_blocked"
    CIRCUIT_BREAKER = "circuit_breaker"


@dataclass(frozen=True, slots=True)
class RuntimeOutcome:
    run_id: RunId
    status: RunStatus
    steps_taken: int = 0
    tokens_used: int = 0
    summary: str = ""
    error: str = ""
    cancellation_reason: CancellationReason | None = None
    verification_status: str = ""

    @classmethod
    def completed(cls, run_id: RunId, steps: int = 0, tokens: int = 0,
                  summary: str = "") -> RuntimeOutcome:
        return cls(
            run_id=run_id, status=RunStatus.COMPLETED,
            steps_taken=steps, tokens_used=tokens, summary=summary,
        )

    @classmethod
    def failed(cls, run_id: RunId, error: str = "",
               steps: int = 0, tokens: int = 0) -> RuntimeOutcome:
        return cls(
            run_id=run_id, status=RunStatus.FAILED,
            error=error, steps_taken=steps, tokens_used=tokens,
        )

    @classmethod
    def cancelled(cls, run_id: RunId,
                  reason: CancellationReason = CancellationReason.USER_REQUESTED,
                  steps: int = 0, tokens: int = 0) -> RuntimeOutcome:
        return cls(
            run_id=run_id, status=RunStatus.CANCELLED,
            cancellation_reason=reason, steps_taken=steps, tokens_used=tokens,
        )
