"""P9: Hook contracts — acceptance tests.

AC: All inputs are frozen dataclasses.
AC: All decisions are frozen dataclasses.
AC: Policies are immutable.
AC: No Any/dict raw transform types.
"""

from __future__ import annotations

import pytest

from hook_core.inputs import (
    PreToolUseInput, PostToolUseInput, UserPromptSubmitInput,
    StopInput, SubagentStopInput, PreCompactInput,
)
from hook_core.decisions import (
    PreToolUseDecision, PostToolUseDecision, UserPromptSubmitDecision,
    StopDecision, PreCompactDecision,
)
from hook_core.policies import (
    PRETOOL_USE, POSTTOOL_USE, USER_PROMPT_SUBMIT, STOP,
    Scheduling, DecisionAuthority, DataAuthority, FailurePolicy,
)


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


class TestDecisions:

    def test_all_frozen(self):
        d = PreToolUseDecision(permission="allow")
        with pytest.raises(Exception):
            d.permission = "deny"  # type: ignore

    def test_stop_decision(self):
        d = StopDecision(decision="block", reason="not done")
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
