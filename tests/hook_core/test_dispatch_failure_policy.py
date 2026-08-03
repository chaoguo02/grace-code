"""Hook dispatch — acceptance tests.

AC: Fail-closed hook blocks on exception.
AC: Fail-open hook continues on exception.
AC: Deny overrides allow.
AC: Defer passes to next hook.
AC: stop_hook_active guard prevents infinite loop.
"""

from __future__ import annotations

import pytest

from hook_core.registry import HookRegistry
from hook_core.dispatcher import HookDispatcher
from hook_core.inputs import (
    PreToolUseInput, StopInput, PostToolUseInput,
    UserPromptSubmitInput, SessionStartInput, NotificationInput,
)
from hook_core.decisions import (
    PermissionDecision,
    PreToolUseDecision, StopDecision, PostToolUseDecision,
    UserPromptSubmitDecision, SessionStartDecision,
)


# ── Handlers ─────────────────────────────────────────────────────────────────

def _allow(_in) -> PreToolUseDecision:
    return PreToolUseDecision(permission=PermissionDecision.ALLOW)

def _deny(_in) -> PreToolUseDecision:
    return PreToolUseDecision(permission=PermissionDecision.DENY, reason="blocked")

def _defer(_in) -> PreToolUseDecision:
    return PreToolUseDecision(permission=PermissionDecision.DEFER)

def _ask(_in) -> PreToolUseDecision:
    return PreToolUseDecision(permission=PermissionDecision.ASK)

def _fail(_in) -> None:
    raise RuntimeError("boom")

def _block_stop(_in) -> StopDecision:
    return StopDecision(decision="block", reason="not done")

def _continue_stop(_in) -> StopDecision:
    return StopDecision(decision="continue")


# ── PreToolUse ───────────────────────────────────────────────────────────────

class TestPreToolUseDispatch:

    def test_allow_passes(self):
        reg = HookRegistry()
        reg.register("allow_hook", "PreToolUse", _allow)
        dispatcher = HookDispatcher(reg)

        result = dispatcher.dispatch(
            "PreToolUse",
            PreToolUseInput(tool_name="Read", tool_input={}),
        )
        assert not result.blocked
        assert result.permission == PermissionDecision.ALLOW

    def test_deny_blocks(self):
        reg = HookRegistry()
        reg.register("deny_hook", "PreToolUse", _deny)
        dispatcher = HookDispatcher(reg)

        result = dispatcher.dispatch(
            "PreToolUse",
            PreToolUseInput(tool_name="Bash", tool_input={}),
        )
        assert result.blocked
        assert "blocked" in result.block_reason
        assert result.permission == PermissionDecision.DENY

    def test_defer_then_allow(self):
        reg = HookRegistry()
        reg.register("d", "PreToolUse", _defer, priority=1)
        reg.register("a", "PreToolUse", _allow, priority=2)
        dispatcher = HookDispatcher(reg)

        result = dispatcher.dispatch(
            "PreToolUse",
            PreToolUseInput(tool_name="Bash", tool_input={}),
        )
        assert not result.blocked
        assert result.permission == PermissionDecision.ALLOW

    def test_defer_then_deny(self):
        reg = HookRegistry()
        reg.register("d", "PreToolUse", _defer, priority=1)
        reg.register("x", "PreToolUse", _deny, priority=2)
        dispatcher = HookDispatcher(reg)

        result = dispatcher.dispatch(
            "PreToolUse",
            PreToolUseInput(tool_name="Bash", tool_input={}),
        )
        assert result.blocked
        assert result.permission == PermissionDecision.DENY

    def test_fail_closed_blocks(self):
        reg = HookRegistry()
        reg.register("failing", "PreToolUse", _fail)
        dispatcher = HookDispatcher(reg)

        result = dispatcher.dispatch(
            "PreToolUse",
            PreToolUseInput(tool_name="Read", tool_input={}),
        )
        # PreToolUse is FAIL_CLOSED → failing hook blocks
        assert result.blocked
        assert "fail-closed" in result.block_reason

    def test_updated_input_merged(self):
        reg = HookRegistry()
        def _add_x(_in):
            return PreToolUseDecision(
                permission=PermissionDecision.ALLOW, updated_input={"x": 1},
            )
        def _add_y(_in):
            return PreToolUseDecision(
                permission=PermissionDecision.ALLOW, updated_input={"y": 2},
            )
        reg.register("a", "PreToolUse", _add_x)
        reg.register("b", "PreToolUse", _add_y)
        dispatcher = HookDispatcher(reg)
        result = dispatcher.dispatch(
            "PreToolUse", PreToolUseInput(tool_name="Bash", tool_input={}),
        )
        assert result.updated_input == {"x": 1, "y": 2}


# ── Stop ─────────────────────────────────────────────────────────────────────

class TestStopDispatch:

    def test_block_stop(self):
        reg = HookRegistry()
        reg.register("stop_check", "Stop", _block_stop)
        dispatcher = HookDispatcher(reg)

        result = dispatcher.dispatch(
            "Stop", StopInput(steps_taken=3),
        )
        assert result.blocked
        assert "not done" in result.block_reason

    def test_continue_stop(self):
        reg = HookRegistry()
        reg.register("ok_stop", "Stop", _continue_stop)
        dispatcher = HookDispatcher(reg)

        result = dispatcher.dispatch(
            "Stop", StopInput(steps_taken=5),
        )
        assert not result.blocked

    def test_stop_hook_active_prevents_block(self):
        reg = HookRegistry()
        reg.register("stop_check", "Stop", _block_stop)
        dispatcher = HookDispatcher(reg)

        result = dispatcher.dispatch(
            "Stop", StopInput(stop_hook_active=True, steps_taken=3),
        )
        assert not result.blocked, "stop_hook_active should prevent blocking"


# ── PostToolUse ──────────────────────────────────────────────────────────────

class TestPostToolUseDispatch:

    def test_fail_open_continues(self):
        reg = HookRegistry()
        reg.register("failing", "PostToolUse", _fail)
        dispatcher = HookDispatcher(reg)

        result = dispatcher.dispatch(
            "PostToolUse",
            PostToolUseInput(tool_name="Bash", tool_input={}),
        )
        assert not result.blocked
        assert len(result.warnings) >= 1

    def test_additional_context_accumulated(self):
        def _ctx_a(_in):
            return PostToolUseDecision(additional_context="a")
        def _ctx_b(_in):
            return PostToolUseDecision(additional_context="b")
        reg = HookRegistry()
        reg.register("a", "PostToolUse", _ctx_a)
        reg.register("b", "PostToolUse", _ctx_b)
        dispatcher = HookDispatcher(reg)
        result = dispatcher.dispatch(
            "PostToolUse", PostToolUseInput(tool_name="Bash", tool_input={}),
        )
        assert "a" in result.additional_context
        assert "b" in result.additional_context


# ── Notification (observe-only) ──────────────────────────────────────────────

class TestNotificationDispatch:

    def test_notification_not_blockable(self):
        reg = HookRegistry()
        reg.register("n", "Notification", _fail)
        dispatcher = HookDispatcher(reg)
        result = dispatcher.dispatch(
            "Notification", NotificationInput(message="test"),
        )
        assert not result.blocked
