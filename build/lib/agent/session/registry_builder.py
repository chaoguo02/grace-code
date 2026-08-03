"""Registry builder — assembles per-session tool registries.

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

import logging
from typing import TYPE_CHECKING

from config.env import SubagentSafetyLimits

if TYPE_CHECKING:
    from agent.session.models import AgentDefinition, SessionRecord
    from core.base import ToolRegistry

logger = logging.getLogger(__name__)


def attach_delegation_tools(
    registry: "ToolRegistry",
    spec: "AgentDefinition",
    session: "SessionRecord",
    *,
    agent_registry,
    runtime,
    circuit_breaker=None,
    mode_policy=None,
) -> "ToolRegistry":
    """Attach session-bound delegation controls gated by ModeExecutionPolicy.

    Tool set per mode (from design doc Section 6.2):

        Plan:
          Agent (foreground only) + control tools

        Build:
          Agent (foreground only) + control tools + worktree tools

        Multi-Agent:
          Agent + AgentBatch + control tools + worktree tools

        Worker:
          none — this function is NOT called for workers.

    """
    configured_depth = min(
        SubagentSafetyLimits.from_environment().max_spawn_depth,
        session.agent_depth.MAX_SUBAGENT_DEPTH,
    )
    delegatable_children = (
        agent_registry.delegatable_by(spec)
        if session.agent_depth.value < configured_depth
        else []
    )
    if not delegatable_children:
        logger.debug(
            "attach_delegation_tools: no delegatable children for agent=%s depth=%s",
            spec.name, session.agent_depth.value,
        )
        return registry

    from agent.session.models import DelegationScope, WorkspaceMode
    from agent.session.task_tool import AgentTool
    from core.base import ToolEffect

    delegation_effect = (
        ToolEffect.DELEGATE_READ_ONLY
        if spec.effective_delegation_scope is DelegationScope.READ_ONLY
        else ToolEffect.DELEGATE_WRITE
    )

    # ── Derive delegation policy flags ──
    _is_multi_agent = False
    _batch_allowed = False
    if mode_policy is not None:
        _is_multi_agent = mode_policy.product_mode == "multi-agent"
        _batch_allowed = mode_policy.agent_batch_allowed
    else:
        raise RuntimeError(
            "attach_delegation_tools requires mode_policy. "
            "Production paths must pass a ModeExecutionPolicy. "
            "Tests should create one explicitly with "
            "ModeExecutionPolicy.for_run(product_mode=..., primary_agent=...)."
        )

    # ── Agent tool (all modes that delegate) ──
    if "Agent" not in registry:
        _agent_tool = AgentTool(
            runtime, session.id,
            caller_agent_name=spec.name,
            circuit_breaker=circuit_breaker,
        )
        # Serial modes: restrict schema to foreground only (Step 7)
        if mode_policy is not None and not _is_multi_agent:
            _agent_tool._restrict_to_foreground = True
        registry.register(_agent_tool)

    # ── AgentBatch — Multi-Agent only ──
    if _batch_allowed and "AgentBatch" not in registry:
        from agent.session.agent_batch_tool import AgentBatchTool
        registry.register(AgentBatchTool(
            runtime,
            session.id,
            caller_agent_name=spec.name,
        ))

    # ── Child control tools (all modes that delegate) ──
    from agent.session.agent_control_tool import (
        AgentControlTool,
        CancelAgentTool,
        SendMessageTool,
        WaitForAgentTool,
    )
    if "SendMessage" not in registry:
        registry.register(SendMessageTool(
            runtime, session.id,
            delegation_effect=delegation_effect,
        ))
    if "WaitForAgent" not in registry:
        registry.register(WaitForAgentTool(
            runtime, session.id,
            delegation_effect=delegation_effect,
        ))
    if "CancelAgent" not in registry:
        registry.register(CancelAgentTool(
            runtime, session.id,
            delegation_effect=delegation_effect,
        ))
    if "agent_control" not in registry:
        registry.register(AgentControlTool(
            runtime, session.id,
            delegation_effect=delegation_effect,
        ))

    # ── Worktree tools (Build + Multi-Agent) ──
    if any(
        child.workspace_mode is WorkspaceMode.WORKTREE
        for child in delegatable_children
    ):
        from agent.session.worktree_tool import (
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
    mode_policy=None,
    mcp_tool_names: frozenset[str] = frozenset(),
    permission_mode_override: str = "",
) -> "ToolRegistry":
    """Build a permission-scoped tool registry for a session.

    All agents go through the same path:
      declared = agent_registry.tool_names_for(spec.name)
      registry = base_registry.filtered(declared | mcp_tool_names)

    All tools are available. Permissions are restricted at execution time
    by PhasePolicy (e.g., analysis tasks get read-only shell).
    """
    from core.policy_registry import PolicyAwareToolRegistry
    from core.policy import PhasePolicy
    from agent.session.models import AgentKind, SessionMode

    declared = agent_registry.tool_names_for(spec.name)

    # ── Set workspace_root on all WorkspaceAware tools (Protocol, not hasattr) ──
    _ws = getattr(session, "repo_path", None)
    if not _ws:
        raise ValueError("Session registry requires an explicit repo_path")
    from core.base import ExecutionContext, ToolRole
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
        mode_policy=mode_policy,
    )
    # Tag registry with session_id for per-session intercept dedup
    registry = registry.with_session_id(session.id)

    # Per-session HookDispatcher: clone global registry, add agent-scoped hooks
    _session_dispatcher = None
    _global_dispatcher = base_registry.hook_dispatcher
    if _global_dispatcher is not None:
        _session_registry = _global_dispatcher.clone_registry()
        # Register agent-scoped hooks on the session registry
        if spec.hooks:
            from hooks.events import HookEvent
            from hooks.registry import ExternalHookConfig
            from hooks.matcher import HookMatcher
            for hook_group in spec.hooks:
                if not isinstance(hook_group, dict):
                    continue
                for event_name_str, hooks_list in hook_group.items():
                    try:
                        event = HookEvent(event_name_str)
                    except ValueError:
                        continue
                    if not isinstance(hooks_list, list):
                        continue
                    for hook_def in hooks_list:
                        if not isinstance(hook_def, dict):
                            continue
                        command = hook_def.get("command", "")
                        if not command:
                            continue
                        matcher = hook_def.get("matcher", "*")
                        config = ExternalHookConfig(
                            command=command,
                            timeout=int(hook_def.get("timeout", 60)),
                            matcher=HookMatcher(pattern=matcher),
                        )
                        _session_registry.register_external(event, config)
        _session_dispatcher = _global_dispatcher.derive(_session_registry)
        # Set per-session dispatcher on the scoped registry
        registry.attach_hook_dispatcher(_session_dispatcher)

    wrapped = PolicyAwareToolRegistry(
        base=registry,
        phase_policy=PhasePolicy(
            allowed_tools=frozenset(registry.tool_names),
            permission_mode=permission_mode_override or spec.permission_mode,
        ),
        repo_path=session.repo_path,
        phase_name="v2_execution",
    )
    # Sync permission_mode to PermissionPipeline (CC-aligned Step 4)
    if spec.permission_mode:
        from hitl.pipeline import PermissionSessionConfig
        wrapped.configure_permission_session(
            PermissionSessionConfig(mode=spec.permission_mode),
        )
    return wrapped
