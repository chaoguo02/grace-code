"""
P9: Hook policies — per-event scheduling, decision authority, data authority, failure.

No Any/dict.  Each hook event type has a declared policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Scheduling(StrEnum):
    AWAITED = "awaited"       # Caller blocks until hook completes
    BACKGROUND = "background"  # Fire-and-forget


class DecisionAuthority(StrEnum):
    BLOCKABLE = "blockable"   # Hook can deny/block the operation
    OBSERVE = "observe"       # Hook can only observe, not block


class DataAuthority(StrEnum):
    TRANSFORM = "transform"   # Hook can modify downstream data
    OBSERVE = "observe"       # Hook is read-only


class FailurePolicy(StrEnum):
    FAIL_CLOSED = "fail_closed"   # Hook error/timeout → block operation
    FAIL_OPEN = "fail_open"       # Hook error → operation continues
    FAIL_TURN = "fail_turn"       # Hook error → terminate current turn


@dataclass(frozen=True, slots=True)
class HookPolicy:
    scheduling: Scheduling
    decision_authority: DecisionAuthority
    data_authority: DataAuthority
    failure_policy: FailurePolicy
    timeout_s: float = 30.0


# ── Default policies per hook event ─────────────────────────────────────────

PRETOOL_USE = HookPolicy(
    scheduling=Scheduling.AWAITED,
    decision_authority=DecisionAuthority.BLOCKABLE,
    data_authority=DataAuthority.TRANSFORM,
    failure_policy=FailurePolicy.FAIL_CLOSED,
)

POSTTOOL_USE = HookPolicy(
    scheduling=Scheduling.AWAITED,
    decision_authority=DecisionAuthority.OBSERVE,
    data_authority=DataAuthority.TRANSFORM,
    failure_policy=FailurePolicy.FAIL_OPEN,
)

USER_PROMPT_SUBMIT = HookPolicy(
    scheduling=Scheduling.AWAITED,
    decision_authority=DecisionAuthority.BLOCKABLE,
    data_authority=DataAuthority.OBSERVE,
    failure_policy=FailurePolicy.FAIL_OPEN,
)

STOP = HookPolicy(
    scheduling=Scheduling.AWAITED,
    decision_authority=DecisionAuthority.BLOCKABLE,
    data_authority=DataAuthority.OBSERVE,
    failure_policy=FailurePolicy.FAIL_OPEN,
    timeout_s=5.0,
)

SUBAGENT_STOP = HookPolicy(
    scheduling=Scheduling.AWAITED,
    decision_authority=DecisionAuthority.BLOCKABLE,
    data_authority=DataAuthority.OBSERVE,
    failure_policy=FailurePolicy.FAIL_OPEN,
)

PRECOMPACT = HookPolicy(
    scheduling=Scheduling.AWAITED,
    decision_authority=DecisionAuthority.BLOCKABLE,
    data_authority=DataAuthority.OBSERVE,
    failure_policy=FailurePolicy.FAIL_OPEN,
)
