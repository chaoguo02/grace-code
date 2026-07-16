"""Registry builder — assembles per-session tool registries for v2 agents.

Architecture:
  1. AgentDefinition.tools → what the agent declares (.md config)
  2. registry.filtered(declared) → visibility control (UX, not security)
  3. PolicyAwareToolRegistry → permission enforcement at execution time
  4. FileWrite/Edit tools → is_path_safe() hard check (security boundary)

Tool visibility is declarative (definition.tools), not gatekept by a separate
security layer. Security is enforced by PermissionPipeline at call time and
by path safety checks inside file tools.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.v2.models import AgentDefinition, SessionRecord
    from tools.base import ToolRegistry


def attach_delegation_tools(
    registry: "ToolRegistry",
    spec: "AgentDefinition",
    session: "SessionRecord",
    *,
    agent_registry,
    runtime,
    circuit_breaker=None,
) -> "ToolRegistry":
    """Attach session-bound delegation controls when declared and in depth."""
    delegatable_children = (
        agent_registry.delegatable_by(spec)
        if session.agent_depth.can_spawn
        else []
    )
    if not delegatable_children:
        return registry

    from agent.v2.models import DelegationScope, WorkspaceMode
    from agent.v2.task_tool import AgentTool
    from tools.base import ToolEffect

    delegation_effect = (
        ToolEffect.DELEGATE_READ_ONLY
        if spec.effective_delegation_scope is DelegationScope.READ_ONLY
        else ToolEffect.DELEGATE_WRITE
    )
    # Idempotent: scoped subagent registries may inherit these from parent
    if "Agent" not in registry:
        registry.register(AgentTool(
            runtime, session.id,
            caller_agent_name=spec.name,
            circuit_breaker=circuit_breaker,
        ))
    from agent.v2.agent_control_tool import AgentControlTool
    if "agent_control" not in registry:
        registry.register(AgentControlTool(
            runtime,
            session.id,
            delegation_effect=delegation_effect,
        ))

    if any(
        child.workspace_mode is WorkspaceMode.WORKTREE
        for child in delegatable_children
    ):
        from agent.v2.worktree_tool import (
            SubagentWorktreeApplyTool,
            SubagentWorktreeDiscardTool,
            SubagentWorktreeInspectTool,
            SubagentWorktreeRetainTool,
        )
        registry.register(SubagentWorktreeInspectTool(runtime, session.id))
        registry.register(SubagentWorktreeApplyTool(runtime, session.id))
        registry.register(SubagentWorktreeDiscardTool(runtime, session.id))
        registry.register(SubagentWorktreeRetainTool(runtime, session.id))
    return registry


def build_registry_for_session(
    spec: "AgentDefinition",
    session,
    *,
    base_registry: "ToolRegistry",
    agent_registry,
    circuit_breaker=None,
    runtime=None,
    mcp_tool_names: frozenset[str] = frozenset(),
) -> "ToolRegistry":
    """Build a permission-scoped tool registry for a v2 session.

    All agents go through the same path:
      declared = agent_registry.tool_names_for(spec.name)
      registry = base_registry.filtered(declared | mcp_tool_names)

    All tools are available. Permissions are restricted at execution time
    by PhasePolicy (e.g., analysis tasks get read-only shell).
    """
    from agent.policy_registry import PolicyAwareToolRegistry
    from agent.policy import PhasePolicy
    from agent.v2.models import AgentKind, SessionMode

    declared = agent_registry.tool_names_for(spec.name)

    # ── Set workspace_root on all WorkspaceAware tools (Protocol, not hasattr) ──
    _ws = getattr(session, "repo_path", None)
    if not _ws:
        raise ValueError("Session registry requires an explicit repo_path")
    from tools.base import ExecutionContext, ToolRole
    registry = base_registry.scoped(ExecutionContext(
        workspace_root=str(_ws), repo_path=str(_ws),
    ))
    if session.mode is SessionMode.SUBAGENT:
        registry = registry.with_permission_request_origin(
            AgentKind.FORK.value
            if session.agent_kind is AgentKind.FORK
            else spec.name
        )
    registry = registry.filtered(declared | mcp_tool_names).excluding_roles(
        frozenset({ToolRole.DELEGATE})
    )

    attach_delegation_tools(
        registry, spec, session,
        agent_registry=agent_registry,
        runtime=runtime,
        circuit_breaker=circuit_breaker,
    )

    # Tag registry with session_id for per-session intercept dedup
    registry._session_id = session.id

    wrapped = PolicyAwareToolRegistry(
        base=registry,
        phase_policy=PhasePolicy(
            allowed_tools=frozenset(registry.tool_names),
            permission_mode=spec.permission_mode,
        ),
        repo_path=session.repo_path,
        phase_name="v2_execution",
    )
    # Sync permission_mode to PermissionPipeline (CC-aligned Step 4)
    if spec.permission_mode and getattr(wrapped, '_permission_pipeline', None) is not None:
        wrapped._permission_pipeline.set_permission_mode(spec.permission_mode)
    return wrapped
