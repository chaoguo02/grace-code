"""Security-boundary regression tests for permissions, hooks, and tool execution."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import MethodType

from config.schema import (
    ResourceGovernanceConfig,
    ResourceGovernanceQueueConfig,
    ResourceGovernanceToolConfig,
)
from core.base import BaseTool, ToolMetadata, ToolRegistry, ToolResult
from core.errors import ToolErrorType, ToolRetryDirective
from core.resource_governor import (
    ResourceGovernor,
    ResourceKind,
    ResourceRequest,
)
from core.types import RetryMode, RetryPolicy, ToolEffect
from hitl.permission_rule import PermissionRule
from hitl.pipeline import (
    PermissionDecision,
    PermissionLayer,
    PermissionPipeline,
    ToolApprovalMode,
)
from hooks.dispatcher import HookDispatcher
from hooks.events import HookContext, HookEvent
from hooks.protocol import DispatchResult, HookControl
from hooks.registry import HookRegistry, InternalHook


class _GuardedTool(BaseTool):
    metadata = ToolMetadata()

    @property
    def name(self) -> str:
        return "Guarded"

    @property
    def description(self) -> str:
        return "Test-only guarded tool."

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

    def permission_denial_reason(self, params: dict) -> str | None:
        if params.get("value") == "danger":
            return "dangerous value"
        return None

    def execute(self, params: dict) -> ToolResult:
        return ToolResult(success=True, output=str(params["value"]))


class _WriteTool(_GuardedTool):
    @property
    def name(self) -> str:
        return "Write"


class _MCPTool(_GuardedTool):
    is_mcp = True

    @property
    def name(self) -> str:
        return "MCPTest"


class _UpdatingDispatcher:
    def __init__(self, *, control: HookControl = HookControl.CONTINUE) -> None:
        self._control = control

    def dispatch(self, event: HookEvent, context: HookContext) -> DispatchResult:
        assert event is HookEvent.PRE_TOOL_USE
        value = str(context.tool_input.get("value", ""))
        return DispatchResult(
            control=self._control,
            updated_input={"value": f"{value}-hook"},
        )


def test_mcp_tool_uses_shared_governor_and_surfaces_typed_capacity_failure():
    governor = ResourceGovernor(ResourceGovernanceConfig(
        mode="enforce",
        queue=ResourceGovernanceQueueConfig(
            max_size=4, timeout_seconds=0.01,
        ),
        tool=ResourceGovernanceToolConfig(global_max=1, per_root_max=1),
    ))
    held = governor.admit(ResourceRequest(
        "held-tool", "root", "other",
        resources={ResourceKind.TOOL_SLOT: 1},
    ))
    registry = ToolRegistry()
    registry.register(_MCPTool())
    registry.attach_resource_governor(
        governor,
        root_session_resolver=lambda _: "root",
    )
    result = registry.with_session_id("session").execute_tool(
        "MCPTest", {"value": "ok"},
    )

    assert result.success is False
    assert result.error is not None
    assert result.metadata["resource_failure"] == "capacity_timeout"
    assert result.metadata["resource_failure_category"] == "thread_capacity"
    held.lease.release()


def test_hook_rewrite_is_revalidated_by_absolute_safety_layer() -> None:
    class _DangerousRewriteDispatcher:
        def dispatch(self, event, context):
            return DispatchResult(updated_input={"value": "danger"})

    pipeline = PermissionPipeline(
        hook_dispatcher=_DangerousRewriteDispatcher(),
        approval_mode=ToolApprovalMode.AUTO,
    )

    result = pipeline.check(_GuardedTool(), {"value": "safe"})

    assert result.decision is PermissionDecision.DENY
    assert result.layer is PermissionLayer.INPUT_VALIDATION
    assert "dangerous value" in result.reason


def test_hook_approve_cannot_override_deny_rule() -> None:
    deny = PermissionRule.parse("Write", tier="deny")
    pipeline = PermissionPipeline(
        rules=[deny],
        hook_dispatcher=_UpdatingDispatcher(control=HookControl.APPROVE),
        approval_mode=ToolApprovalMode.AUTO,
    )

    result = pipeline.check(_WriteTool(), {"value": "safe"})

    assert result.decision is PermissionDecision.DENY
    assert result.layer is PermissionLayer.RULE


def test_hook_rewrite_is_revalidated_against_tool_schema_before_execution() -> None:
    executed = []

    class _InvalidTypeDispatcher:
        def dispatch(self, event, context):
            return DispatchResult(updated_input={"value": 42})

    class _RecordingTool(_GuardedTool):
        def execute(self, params: dict) -> ToolResult:
            executed.append(params)
            return super().execute(params)

    pipeline = PermissionPipeline(
        hook_dispatcher=_InvalidTypeDispatcher(),
        approval_mode=ToolApprovalMode.AUTO,
    )
    registry = ToolRegistry(permission_pipeline=pipeline)
    registry.register(_RecordingTool())

    result = registry.execute_tool("Guarded", {"value": "safe"})

    assert not result.success
    assert result.tool_error is not None
    assert result.tool_error.error_type.value == "invalid_params"
    assert ("must be a string" in (result.error or "")
            or "not of type" in (result.error or ""))
    assert executed == []


def test_parallel_permission_checks_do_not_share_hook_updates() -> None:
    pipeline = PermissionPipeline(
        hook_dispatcher=_UpdatingDispatcher(),
        approval_mode=ToolApprovalMode.AUTO,
    )
    both_in_rules = Barrier(2)
    original_layer3 = pipeline._layer3_rules

    def synchronized_layer3(self, tool_name, params):
        both_in_rules.wait(timeout=2)
        return original_layer3(tool_name, params)

    pipeline._layer3_rules = MethodType(synchronized_layer3, pipeline)

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(pipeline.check, _GuardedTool(), {"value": "a"})
        future_b = pool.submit(pipeline.check, _GuardedTool(), {"value": "b"})
        result_a = future_a.result(timeout=3)
        result_b = future_b.result(timeout=3)

    assert result_a.updated_params == {"value": "a-hook"}
    assert result_b.updated_params == {"value": "b-hook"}


def test_post_hook_context_is_typed_and_raw_output_is_unchanged() -> None:
    class _PostContextDispatcher:
        def dispatch(self, event, context):
            if event is HookEvent.POST_TOOL_USE:
                return DispatchResult(additional_context="verify this result")
            return DispatchResult()

    dispatcher = _PostContextDispatcher()
    pipeline = PermissionPipeline(
        hook_dispatcher=dispatcher,
        approval_mode=ToolApprovalMode.AUTO,
    )
    registry = ToolRegistry(
        permission_pipeline=pipeline,
        hook_dispatcher=dispatcher,
    ).register(_GuardedTool())

    result = registry.execute_tool("Guarded", {"value": "raw"})
    observation = result.to_observation("Guarded")
    from agent.observation_rendering import build_tool_result_content
    rendered = build_tool_result_content(observation)

    assert result.output == "raw"
    assert len(result.attachments) == 1
    assert "verify this result" in rendered
    assert "[Hook Context | source: hook]" in rendered


def test_blockable_internal_hook_failure_fails_closed() -> None:
    registry = HookRegistry()

    def broken_hook(context: HookContext) -> None:
        raise RuntimeError("internal policy unavailable")

    registry.register_internal(
        HookEvent.PRE_TOOL_USE,
        InternalHook(callback=broken_hook),
    )
    dispatcher = HookDispatcher(registry)

    result = dispatcher.dispatch(
        HookEvent.PRE_TOOL_USE,
        HookContext(
            event=HookEvent.PRE_TOOL_USE,
            tool_name="Write",
            tool_input={"value": "safe"},
        ),
    )

    assert result.control is HookControl.BLOCK
    assert "internal policy unavailable" in result.reason


def test_notification_internal_hook_failure_is_observable_and_non_blocking() -> None:
    registry = HookRegistry()

    def broken_hook(context: HookContext) -> None:
        raise RuntimeError("telemetry unavailable")

    registry.register_internal(
        HookEvent.POST_RESPONSE,
        InternalHook(callback=broken_hook),
    )
    dispatcher = HookDispatcher(registry)

    result = dispatcher.dispatch(
        HookEvent.POST_RESPONSE,
        HookContext(event=HookEvent.POST_RESPONSE),
    )

    assert result.control is HookControl.CONTINUE
    assert result.warnings
    assert "telemetry unavailable" in result.warnings[0]


def test_read_only_retry_uses_one_stable_invocation_id_until_success() -> None:
    class _TransientRead(_GuardedTool):
        metadata = ToolMetadata(
            effects=frozenset({ToolEffect.READ_WORKSPACE}),
            retry_policy=RetryPolicy(
                mode=RetryMode.AUTOMATIC,
                max_attempts=3,
                base_delay_ms=0,
                max_delay_ms=0,
            ),
        )

        def __init__(self) -> None:
            self.calls = 0

        def execute(self, params: dict) -> ToolResult:
            self.calls += 1
            if self.calls == 1:
                return ToolResult.from_error(
                    ToolErrorType.TIMEOUT,
                    "temporary timeout",
                    retry=ToolRetryDirective.RETRY,
                )
            return ToolResult(success=True, output="recovered")

    tool = _TransientRead()
    result = ToolRegistry().register(tool).execute_tool(
        "Guarded", {"value": "safe"}, invocation_id="stable-call"
    )

    assert result.success
    assert result.invocation_id == "stable-call"
    assert result.attempt_count == 2
    assert result.eventual_success is True
    assert [item["attempt"] for item in result.metadata["attempts"]] == [1, 2]


def test_side_effect_retry_is_blocked_without_interactive_approval() -> None:
    class _TransientWrite(_WriteTool):
        metadata = ToolMetadata(
            effects=frozenset({ToolEffect.WRITE_WORKSPACE}),
            retry_policy=RetryPolicy(
                mode=RetryMode.APPROVAL,
                max_attempts=2,
                base_delay_ms=0,
                max_delay_ms=0,
            ),
        )

        def execute(self, params: dict) -> ToolResult:
            return ToolResult.from_error(
                ToolErrorType.UNAVAILABLE,
                "temporary service failure",
                retry=ToolRetryDirective.RETRY,
            )

    result = ToolRegistry().register(_TransientWrite()).execute_tool(
        "Write", {"value": "safe"}, invocation_id="write-call"
    )

    assert not result.success
    assert result.tool_error.error_type is ToolErrorType.PERMISSION_DENIED
    assert result.invocation_id == "write-call"
    assert result.attempt_count == 2
    assert "retry approval is unavailable" in result.error.lower()
