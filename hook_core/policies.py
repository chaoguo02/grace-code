"""
G12: Hook policies — four dimensions, event-type-bound decision class, no fallback.

Each hook event type MUST have an explicit policy in the map.
No default/fallback — unknown event types are a configuration error.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Scheduling(StrEnum):
    AWAITED = "awaited"
    BACKGROUND = "background"


class DecisionAuthority(StrEnum):
    BLOCKABLE = "blockable"
    OBSERVE = "observe"


class DataAuthority(StrEnum):
    TRANSFORM = "transform"
    OBSERVE = "observe"


class FailurePolicy(StrEnum):
    FAIL_CLOSED = "fail_closed"
    FAIL_OPEN = "fail_open"
    FAIL_TURN = "fail_turn"
    EVENT_DEFAULT = "event_default"  # blockable→FAIL_CLOSED, non-blockable→FAIL_OPEN


@dataclass(frozen=True, slots=True)
class HookPolicy:
    """Four-dimensional hook policy bound to a lifecycle event."""
    scheduling: Scheduling
    decision_authority: DecisionAuthority
    data_authority: DataAuthority
    failure_policy: FailurePolicy
    timeout_s: float = 30.0


# ── Per-event policies (G12: exhaustive, no fallback) ────────────────────

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

# ── G12: Exhaustive policy map — no fallback ───────────────────────────

_POLICY_MAP: dict[str, HookPolicy] = {
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


def policy_for(event_type: str) -> HookPolicy:
    """Return the policy for *event_type*.  Raises KeyError if unknown.

    G12: No fallback/default.  Every event type must have an explicit policy.
    """
    if event_type not in _POLICY_MAP:
        raise KeyError(
            f"No policy defined for event type '{event_type}'. "
            f"Every hook event must have an explicit policy."
        )
    return _POLICY_MAP[event_type]


def known_event_types() -> frozenset[str]:
    """All event types with defined policies."""
    return frozenset(_POLICY_MAP.keys())
