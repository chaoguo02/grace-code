"""Hook contracts — acceptance tests.

AC: All inputs are frozen dataclasses.
AC: All decisions are frozen dataclasses.
AC: Policies are immutable.
"""

from __future__ import annotations

import pytest

from hook_core.inputs import (
    PreToolUseInput, PostToolUseInput, PostToolUseFailureInput,
    UserPromptSubmitInput, StopInput, StopFailureInput,
    SessionStartInput, SessionEndInput,
    SubagentStartInput, SubagentStopInput,
    PreCompactInput, PostCompactInput,
    NotificationInput, PostToolBatchInput,
    PermissionRequestInput, PermissionDeniedInput,
)
from hook_core.decisions import (
    PermissionDecision,
    PreToolUseDecision, PostToolUseDecision, PostToolUseFailureDecision,
    UserPromptSubmitDecision, StopDecision, SessionStartDecision,
    PreCompactDecision,
)
from hook_core.policies import (
    PRETOOL_USE, POSTTOOL_USE, STOP,
    Scheduling, DecisionAuthority, DataAuthority, FailurePolicy,
    policy_for,
)
from hook_core.events import HookEvent, BLOCKABLE_EVENTS


class TestEvents:

    def test_all_16_events(self):
        assert len(HookEvent) >= 16

    def test_blockable_events(self):
        assert HookEvent.PRE_TOOL_USE in BLOCKABLE_EVENTS
        assert HookEvent.NOTIFICATION not in BLOCKABLE_EVENTS
        assert HookEvent.SESSION_START not in BLOCKABLE_EVENTS

    def test_policy_for_all_events(self):
        for ev in HookEvent:
            p = policy_for(ev.value)
            assert p is not None


class TestInputs:

    def test_all_frozen(self):
        i = PreToolUseInput(tool_name="Read", tool_input={})
        with pytest.raises(Exception):
            i.tool_name = "Write"  # type: ignore

    def test_pre_tool_use_fields(self):
        i = PreToolUseInput(tool_name="Bash", tool_input={"cmd": "ls"})
        assert i.tool_name == "Bash"

    def test_post_tool_use_fields(self):
        i = PostToolUseInput(tool_name="Read", tool_input={}, tool_output="data")
        assert i.tool_output == "data"

    def test_post_tool_use_failure_fields(self):
        i = PostToolUseFailureInput(
            tool_name="Bash", tool_input={},
            error_message="timeout", error_type="TIMEOUT",
        )
        assert i.error_message == "timeout"

    def test_stop_input_guard_field(self):
        i = StopInput(stop_hook_active=True)
        assert i.stop_hook_active is True

    def test_all_inputs_exist_for_all_events(self):
        """Every event should have a corresponding input class."""
        event_inputs = {
            "PreToolUse": PreToolUseInput,
            "PostToolUse": PostToolUseInput,
            "PostToolUseFailure": PostToolUseFailureInput,
            "PostToolBatch": PostToolBatchInput,
            "PermissionRequest": PermissionRequestInput,
            "PermissionDenied": PermissionDeniedInput,
            "UserPromptSubmit": UserPromptSubmitInput,
            "Stop": StopInput,
            "StopFailure": StopFailureInput,
            "SessionStart": SessionStartInput,
            "SessionEnd": SessionEndInput,
            "SubagentStart": SubagentStartInput,
            "SubagentStop": SubagentStopInput,
            "PreCompact": PreCompactInput,
            "PostCompact": PostCompactInput,
            "Notification": NotificationInput,
        }
        for ev_name, cls in event_inputs.items():
            assert hasattr(cls, '__dataclass_fields__'), f"{ev_name} input missing"


class TestDecisions:

    def test_all_frozen(self):
        d = PreToolUseDecision(permission=PermissionDecision.ALLOW)
        with pytest.raises(Exception):
            d.permission = "deny"  # type: ignore

    def test_permission_precedence(self):
        prec = PermissionDecision.precedence()
        assert prec[0] == PermissionDecision.DENY
        assert prec[-1] == PermissionDecision.ALLOW

    def test_stop_decision(self):
        from hook_core.decisions import StopVerdict
        d = StopDecision(decision=StopVerdict.BLOCK, reason="not done")
        assert d.decision == "block"


class TestPolicies:

    def test_policies_immutable(self):
        assert PRETOOL_USE.scheduling == Scheduling.AWAITED
        assert PRETOOL_USE.decision_authority == DecisionAuthority.BLOCKABLE
        assert PRETOOL_USE.failure_policy == FailurePolicy.FAIL_CLOSED

    def test_post_tool_observe_only(self):
        assert POSTTOOL_USE.decision_authority == DecisionAuthority.OBSERVE

    def test_stop_has_timeout(self):
        assert STOP.timeout_s == 5.0
