from __future__ import annotations

from core.base import (
    BaseTool,
    ToolEffect,
    ToolMetadata,
    ToolRegistry,
    ToolResult,
)
from core.policy import PhasePolicy
from core.policy_registry import PolicyAwareToolRegistry
from hitl.permission_rule import PermissionRule
from hitl.pipeline import (
    PermissionPipeline,
    PermissionSessionConfig,
    PromptAction,
    PromptDecision,
    ToolApprovalMode,
)
from hooks.events import HookEvent
from hooks.protocol import DispatchResult
from skills.tool import SkillContextModifier


class _WriteTool(BaseTool):
    name = "Write"
    description = "write a file"
    parameters_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.WRITE_WORKSPACE}),
        required_permissions=frozenset({"workspace:write"}),
    )

    def execute(self, params):
        return ToolResult(success=True, output="written")


def test_registry_configures_permission_session_without_private_access():
    pipeline = PermissionPipeline()
    registry = ToolRegistry(permission_pipeline=pipeline).register(_WriteTool())
    deny = PermissionRule.parse("Write", tier="deny")

    registry.configure_permission_session(PermissionSessionConfig(
        mode="acceptEdits",
        rules=(deny,),
        requesting_agent="build",
    ))
    result = registry.execute_tool("Write", {"path": "safe.txt"})

    assert not result.success
    assert "denied by rule" in (result.error or "")
    signal = registry.permission_control_signal()
    assert signal is not None
    assert signal.total_denials == 1


def test_permission_mode_is_authorized_once_at_permission_boundary():
    pipeline = PermissionPipeline()
    base = ToolRegistry(permission_pipeline=pipeline).register(_WriteTool())
    wrapped = PolicyAwareToolRegistry(
        base=base,
        phase_policy=PhasePolicy(permission_mode="plan"),
        repo_path=".",
        phase_name="execution",
    )
    wrapped.configure_permission_session(PermissionSessionConfig(mode="plan"))

    schemas = wrapped.get_schemas()
    result = wrapped.execute_tool("Write", {"path": "safe.txt"})

    assert [schema.name for schema in schemas] == ["Write"]
    assert not result.success
    assert "plan mode" in (result.error or "")


def test_skill_modifier_from_bound_registry_persists_on_run_owner():
    base = ToolRegistry().register(_WriteTool())
    owner = PolicyAwareToolRegistry(
        base=base,
        phase_policy=PhasePolicy(),
        repo_path=".",
        phase_name="execution",
    )
    bound = PolicyAwareToolRegistry(
        base=base,
        phase_policy=owner.phase_policy,
        repo_path=".",
        phase_name="execution",
        modifier_owner=owner,
    )

    bound._apply_skill_modifier(SkillContextModifier(
        disallowed_tools=frozenset({"Write"}),
        model="special-model",
        effort="high",
        context="fork",
    ))

    assert owner.get_schemas() == []
    assert owner.skill_runtime_overrides == {
        "model": "special-model",
        "effort": "high",
        "context": "fork",
    }


def test_permission_request_event_fires_before_interactive_approval():
    events = []

    class _Dispatcher:
        def dispatch(self, event, context):
            if event is HookEvent.PERMISSION_REQUEST:
                events.append((
                    "hook",
                    event,
                    context.tool_name,
                    context.required_permissions,
                ))
            return DispatchResult()

    def _confirm(request):
        events.append((
            "confirm",
            request.tool_name,
            request.required_permissions,
        ))
        return PromptDecision(action=PromptAction.ALLOW_ONCE)

    pipeline = PermissionPipeline(
        hook_dispatcher=_Dispatcher(),
        confirm_callback=_confirm,
        approval_mode=ToolApprovalMode.PROMPT,
    )

    result = pipeline.check(_WriteTool(), {"path": "safe.txt"})

    assert result.approved
    assert events == [(
        "hook",
        HookEvent.PERMISSION_REQUEST,
        "Write",
        frozenset({"workspace:write"}),
    ), (
        "confirm",
        "Write",
        frozenset({"workspace:write"}),
    )]
