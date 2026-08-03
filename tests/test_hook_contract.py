"""G36M-final: Tests old hooks.* package (deprecated). New tests in tests/hook_core/."""

from pathlib import Path

import pytest

from core.base import BaseTool, ToolRegistry, ToolResult
from entry.bootstrap.hook_bootstrap import init_hook_dispatcher
from hooks.dispatcher import HookDispatcher
from hooks.events import HookContext, HookEvent
from hooks.protocol import HookControl, HookDecision, HookOutput
from hooks.registry import (
    HookDataAuthority,
    HookDecisionAuthority,
    HookFailurePolicy,
    HookRegistry,
    HookScheduling,
    InternalHook,
)


def _context(event: HookEvent = HookEvent.PRE_TOOL_USE) -> HookContext:
    return HookContext(event=event, tool_name="Write", tool_input={})


def test_later_deny_dominates_earlier_approval() -> None:
    registry = HookRegistry()
    registry.register_internal(HookEvent.PRE_TOOL_USE, InternalHook(
        callback=lambda _: HookOutput(decision=HookDecision.ALLOW),
        hook_id="allow-first",
        priority=10,
    ))
    registry.register_internal(HookEvent.PRE_TOOL_USE, InternalHook(
        callback=lambda _: HookOutput(
            decision=HookDecision.BLOCK,
            reason="policy denied",
        ),
        hook_id="deny-later",
        priority=20,
    ))

    result = HookDispatcher(registry).dispatch(
        HookEvent.PRE_TOOL_USE,
        _context(),
    )

    assert result.control is HookControl.BLOCK
    assert result.reason == "policy denied"


def test_internal_hook_can_transform_input_context_and_output() -> None:
    registry = HookRegistry()
    registry.register_internal(HookEvent.PRE_TOOL_USE, InternalHook(
        callback=lambda _: {
            "additionalContext": "policy context",
            "updatedInput": {"path": "safe.txt"},
            "updatedOutput": {"output": "sanitized"},
        },
        hook_id="transform",
    ))

    result = HookDispatcher(registry).dispatch(
        HookEvent.PRE_TOOL_USE,
        _context(),
    )

    assert result.updated_input == {"path": "safe.txt"}
    assert result.updated_output == {"output": "sanitized"}
    assert result.additional_context == "policy context"


def test_blockable_hook_failure_is_closed_by_default() -> None:
    registry = HookRegistry()
    registry.register_internal(HookEvent.PRE_TOOL_USE, InternalHook(
        callback=lambda _: (_ for _ in ()).throw(RuntimeError("offline")),
        hook_id="policy-service",
    ))

    result = HookDispatcher(registry).dispatch(
        HookEvent.PRE_TOOL_USE,
        _context(),
    )

    assert result.control is HookControl.BLOCK
    assert "offline" in result.reason


def test_detached_hook_cannot_claim_policy_or_transform_authority() -> None:
    registry = HookRegistry()
    with pytest.raises(ValueError, match="Detached hooks"):
        registry.register_internal(HookEvent.POST_RESPONSE, InternalHook(
            callback=lambda _: None,
            scheduling=HookScheduling.DETACHED,
        ))

    registry.register_internal(HookEvent.POST_RESPONSE, InternalHook(
        callback=lambda _: None,
        scheduling=HookScheduling.DETACHED,
        decision_authority=HookDecisionAuthority.ADVISORY,
        data_authority=HookDataAuthority.READ_ONLY,
        failure_policy=HookFailurePolicy.FAIL_OPEN,
    ))


class _EchoTool(BaseTool):
    @property
    def name(self) -> str:
        return "Echo"

    @property
    def description(self) -> str:
        return "Echo a value."

    @property
    def parameters_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    def execute(self, params: dict) -> ToolResult:
        return ToolResult(success=True, output="raw")


def test_post_tool_hook_applies_authorized_output_transform() -> None:
    registry = HookRegistry()
    registry.register_internal(HookEvent.POST_TOOL_USE, InternalHook(
        callback=lambda _: HookOutput(updated_output="sanitized"),
        hook_id="result-sanitizer",
    ))
    dispatcher = HookDispatcher(registry)
    tools = ToolRegistry(hook_dispatcher=dispatcher).register(_EchoTool())

    result = tools.execute_tool("Echo", {})

    assert result.output == "sanitized"


def test_bootstrap_registers_session_start_context_injector(
    tmp_path: Path,
) -> None:
    (tmp_path / "CLAUDE.md").write_text("project rule", encoding="utf-8")
    dispatcher = init_hook_dispatcher(tmp_path)

    result = dispatcher.dispatch(
        HookEvent.SESSION_START,
        HookContext(event=HookEvent.SESSION_START, session_id="session"),
    )

    assert "project rule" in result.additional_context

