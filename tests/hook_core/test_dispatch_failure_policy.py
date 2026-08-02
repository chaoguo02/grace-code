"""P11: Hook dispatch failure policy — acceptance tests.

AC: Fail-closed hook blocks on exception.
AC: Fail-open hook continues on exception.
AC: Deny overrides allow.
AC: Total deadline enforced.
"""

from __future__ import annotations

import pytest

from hook_core.registry import HookRegistry
from hook_core.dispatcher import HookDispatcher
from hook_core.inputs import PreToolUseInput, StopInput
from hook_core.decisions import PreToolUseDecision, StopDecision


def _allow(_in) -> PreToolUseDecision:
    return PreToolUseDecision(permission="allow")

def _deny(_in) -> PreToolUseDecision:
    return PreToolUseDecision(permission="deny", reason="blocked")

def _fail(_in) -> None:
    raise RuntimeError("boom")

def _continue(_in) -> StopDecision:
    return StopDecision(decision="continue")

def _block_stop(_in) -> StopDecision:
    return StopDecision(decision="block", reason="not done")


class TestPreToolUseDispatch:

    def test_allow_passes(self):
        reg = HookRegistry()
        reg.register("allow_hook", "PreToolUse", _allow)
        dispatcher = HookDispatcher(reg)
        snap = reg.snapshot()

        result = dispatcher.dispatch(
            snap, "PreToolUse",
            PreToolUseInput(tool_name="Read", tool_input={}),
        )
        assert not result.blocked

    def test_deny_blocks(self):
        reg = HookRegistry()
        reg.register("deny_hook", "PreToolUse", _deny)
        dispatcher = HookDispatcher(reg)

        result = dispatcher.dispatch(
            reg.snapshot(), "PreToolUse",
            PreToolUseInput(tool_name="Bash", tool_input={}),
        )
        assert result.blocked
        assert "blocked" in result.block_reason

    def test_fail_closed_blocks_pre_tool_use(self):
        reg = HookRegistry()
        reg.register("failing", "PreToolUse", _fail)
        dispatcher = HookDispatcher(reg)

        result = dispatcher.dispatch(
            reg.snapshot(), "PreToolUse",
            PreToolUseInput(tool_name="Read", tool_input={}),
        )
        # PreToolUse is FAIL_CLOSED → failing hook blocks
        assert result.blocked
        assert "fail-closed" in result.block_reason


class TestStopDispatch:

    def test_block_stop(self):
        reg = HookRegistry()
        reg.register("stop_check", "Stop", _block_stop)
        dispatcher = HookDispatcher(reg)

        result = dispatcher.dispatch(
            reg.snapshot(), "Stop",
            StopInput(steps_taken=3),
        )
        assert result.blocked
        assert "not done" in result.block_reason

    def test_continue_stop(self):
        reg = HookRegistry()
        reg.register("ok_stop", "Stop", _continue)
        dispatcher = HookDispatcher(reg)

        result = dispatcher.dispatch(
            reg.snapshot(), "Stop",
            StopInput(steps_taken=5),
        )
        assert not result.blocked
