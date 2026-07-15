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
    from agent.v2.models import AgentDefinition
    from tools.base import ToolRegistry


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
    from agent.v2.task_tool import AgentTool
    from agent.policy_registry import PolicyAwareToolRegistry
    from agent.policy import PhasePolicy

    declared = agent_registry.tool_names_for(spec.name)

    # ── Set workspace_root on all WorkspaceAware tools (Protocol, not hasattr) ──
    _ws = getattr(session, "repo_path", None)
    if not _ws:
        raise ValueError("Session registry requires an explicit repo_path")
    from tools.base import ExecutionContext
    registry = base_registry.scoped(ExecutionContext(
        workspace_root=str(_ws), repo_path=str(_ws),
    )).filtered(declared | mcp_tool_names)

    # Never expose an unbound base task tool. Effective delegation is the
    # intersection of the parsed allowlist, child visibility, and authority scope.
    registry._tools.pop("task", None)
    delegatable_children = agent_registry.delegatable_by(spec)
    if delegatable_children:
        registry.register(AgentTool(
            runtime, session.id,
            caller_agent_name=spec.name,
            circuit_breaker=circuit_breaker,
        ))

        from agent.v2.models import WorkspaceMode
        has_worktree_child = any(
            child.workspace_mode is WorkspaceMode.WORKTREE
            for child in delegatable_children
        )
        if has_worktree_child:
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

    # Tag registry with session_id for per-session intercept dedup
    registry._session_id = session.id

    wrapped = PolicyAwareToolRegistry(
        base=registry,
        phase_policy=PhasePolicy(allowed_tools=frozenset(registry.tool_names)),
        repo_path=session.repo_path,
        phase_name="v2_execution",
    )
    return wrapped
