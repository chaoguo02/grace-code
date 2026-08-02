"""
CC-aligned hook policies — per-event scheduling, decision authority, data authority, failure.

Each hook event type has a declared default policy.
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
    FAIL_CLOSED = "fail_closed"     # Hook error → block operation
    FAIL_OPEN = "fail_open"         # Hook error → continue
    FAIL_TURN = "fail_turn"         # Hook error → terminate turn
    EVENT_DEFAULT = "event_default" # blockable→FAIL_CLOSED, non-blockable→FAIL_OPEN


@dataclass(frozen=True, slots=True)
class HookPolicy:
    scheduling: Scheduling
    decision_authority: DecisionAuthority
    data_authority: DataAuthority
    failure_policy: FailurePolicy
    timeout_s: float = 30.0


# ── Per-event default policies ──────────────────────────────────────────────

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

POSTTOOL_USE_FAILURE = HookPolicy(
    scheduling=Scheduling.AWAITED,
    decision_authority=DecisionAuthority.OBSERVE,
    data_authority=DataAuthority.OBSERVE,
    failure_policy=FailurePolicy.FAIL_OPEN,
)

POSTTOOL_BATCH = HookPolicy(
    scheduling=Scheduling.AWAITED,
    decision_authority=DecisionAuthority.BLOCKABLE,
    data_authority=DataAuthority.OBSERVE,
    failure_policy=FailurePolicy.FAIL_OPEN,
)

PERMISSION_REQUEST = HookPolicy(
    scheduling=Scheduling.AWAITED,
    decision_authority=DecisionAuthority.BLOCKABLE,
    data_authority=DataAuthority.OBSERVE,
    failure_policy=FailurePolicy.FAIL_OPEN,
)

PERMISSION_DENIED = HookPolicy(
    scheduling=Scheduling.AWAITED,
    decision_authority=DecisionAuthority.OBSERVE,
    data_authority=DataAuthority.OBSERVE,
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

STOP_FAILURE = HookPolicy(
    scheduling=Scheduling.AWAITED,
    decision_authority=DecisionAuthority.OBSERVE,
    data_authority=DataAuthority.OBSERVE,
    failure_policy=FailurePolicy.FAIL_OPEN,
)

SESSION_START = HookPolicy(
    scheduling=Scheduling.AWAITED,
    decision_authority=DecisionAuthority.OBSERVE,
    data_authority=DataAuthority.TRANSFORM,
    failure_policy=FailurePolicy.FAIL_OPEN,
)

SESSION_END = HookPolicy(
    scheduling=Scheduling.AWAITED,
    decision_authority=DecisionAuthority.OBSERVE,
    data_authority=DataAuthority.OBSERVE,
    failure_policy=FailurePolicy.FAIL_OPEN,
)

SUBAGENT_START = HookPolicy(
    scheduling=Scheduling.AWAITED,
    decision_authority=DecisionAuthority.OBSERVE,
    data_authority=DataAuthority.OBSERVE,
    failure_policy=FailurePolicy.FAIL_OPEN,
)

SUBAGENT_STOP = HookPolicy(
    scheduling=Scheduling.AWAITED,
    decision_authority=DecisionAuthority.BLOCKABLE,
    data_authority=DataAuthority.OBSERVE,
    failure_policy=FailurePolicy.FAIL_OPEN,
)

PRE_COMPACT = HookPolicy(
    scheduling=Scheduling.AWAITED,
    decision_authority=DecisionAuthority.BLOCKABLE,
    data_authority=DataAuthority.OBSERVE,
    failure_policy=FailurePolicy.FAIL_OPEN,
)

POST_COMPACT = HookPolicy(
    scheduling=Scheduling.AWAITED,
    decision_authority=DecisionAuthority.OBSERVE,
    data_authority=DataAuthority.OBSERVE,
    failure_policy=FailurePolicy.FAIL_OPEN,
)

NOTIFICATION = HookPolicy(
    scheduling=Scheduling.AWAITED,
    decision_authority=DecisionAuthority.OBSERVE,
    data_authority=DataAuthority.OBSERVE,
    failure_policy=FailurePolicy.FAIL_OPEN,
)


def policy_for(event_type: str) -> HookPolicy:
    """Return the default policy for a hook event type string."""
    _map: dict[str, HookPolicy] = {
        "PreToolUse": PRETOOL_USE,
        "PostToolUse": POSTTOOL_USE,
        "PostToolUseFailure": POSTTOOL_USE_FAILURE,
        "PostToolBatch": POSTTOOL_BATCH,
        "PermissionRequest": PERMISSION_REQUEST,
        "PermissionDenied": PERMISSION_DENIED,
        "UserPromptSubmit": USER_PROMPT_SUBMIT,
        "Stop": STOP,
        "StopFailure": STOP_FAILURE,
        "SessionStart": SESSION_START,
        "SessionEnd": SESSION_END,
        "SubagentStart": SUBAGENT_START,
        "SubagentStop": SUBAGENT_STOP,
        "PreCompact": PRE_COMPACT,
        "PostCompact": POST_COMPACT,
        "Notification": NOTIFICATION,
    }
    return _map.get(
        event_type,
        HookPolicy(
            scheduling=Scheduling.AWAITED,
            decision_authority=DecisionAuthority.OBSERVE,
            data_authority=DataAuthority.OBSERVE,
            failure_policy=FailurePolicy.FAIL_OPEN,
        ),
    )
