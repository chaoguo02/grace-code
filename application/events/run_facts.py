"""
P2: Run fact payloads — independent classes, no dict passthrough.

Each terminal status has its own payload class with only the fields
relevant to that outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from core.eventing.identifiers import RunId


class RunTerminalStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    GAVE_UP = "gave_up"


# ── Lifecycle facts ─────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class RunSubmittedV1:
    run_id: RunId
    turn_index: int = 0

    def __post_init__(self) -> None:
        if self.turn_index < 0:
            raise ValueError(f"turn_index must be >= 0, got {self.turn_index}")


@dataclass(frozen=True, slots=True)
class RunStartedV1:
    run_id: RunId
    turn_index: int = 0


# ── Terminal facts (one class per status — no dict) ─────────────────────────

@dataclass(frozen=True, slots=True)
class RunCompletedV1:
    run_id: RunId
    turn_index: int = 0
    steps_taken: int = 0
    tokens_used: int = 0
    summary: str = ""

    def __post_init__(self) -> None:
        if self.steps_taken < 0:
            raise ValueError(f"steps_taken must be >= 0, got {self.steps_taken}")
        if self.tokens_used < 0:
            raise ValueError(f"tokens_used must be >= 0, got {self.tokens_used}")


@dataclass(frozen=True, slots=True)
class RunFailedV1:
    run_id: RunId
    turn_index: int = 0
    error: str = ""
    steps_taken: int = 0
    tokens_used: int = 0


@dataclass(frozen=True, slots=True)
class RunCancelledV1:
    run_id: RunId
    turn_index: int = 0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RunBlockedV1:
    run_id: RunId
    turn_index: int = 0
    blocked_by: str = ""  # hook name, policy name, or "governor"
    detail: str = ""


@dataclass(frozen=True, slots=True)
class RunGaveUpV1:
    run_id: RunId
    turn_index: int = 0
    consecutive_failures: int = 0
    max_steps_reached: bool = False


# ── Factory helpers ─────────────────────────────────────────────────────────

def submitted(run_id: str, turn_index: int = 0) -> RunSubmittedV1:
    return RunSubmittedV1(run_id=RunId(run_id), turn_index=turn_index)

def started(run_id: str, turn_index: int = 0) -> RunStartedV1:
    return RunStartedV1(run_id=RunId(run_id), turn_index=turn_index)

def completed(run_id: str, turn_index: int = 0,
              steps_taken: int = 0, tokens_used: int = 0,
              summary: str = "") -> RunCompletedV1:
    return RunCompletedV1(
        run_id=RunId(run_id), turn_index=turn_index,
        steps_taken=steps_taken, tokens_used=tokens_used, summary=summary,
    )

def failed(run_id: str, turn_index: int = 0,
           error: str = "", steps_taken: int = 0,
           tokens_used: int = 0) -> RunFailedV1:
    return RunFailedV1(
        run_id=RunId(run_id), turn_index=turn_index,
        error=error, steps_taken=steps_taken, tokens_used=tokens_used,
    )

def cancelled(run_id: str, turn_index: int = 0,
              reason: str = "") -> RunCancelledV1:
    return RunCancelledV1(run_id=RunId(run_id), turn_index=turn_index, reason=reason)

def blocked(run_id: str, turn_index: int = 0,
            blocked_by: str = "", detail: str = "") -> RunBlockedV1:
    return RunBlockedV1(
        run_id=RunId(run_id), turn_index=turn_index,
        blocked_by=blocked_by, detail=detail,
    )

def gave_up(run_id: str, turn_index: int = 0,
            consecutive_failures: int = 0,
            max_steps_reached: bool = False) -> RunGaveUpV1:
    return RunGaveUpV1(
        run_id=RunId(run_id), turn_index=turn_index,
        consecutive_failures=consecutive_failures,
        max_steps_reached=max_steps_reached,
    )
