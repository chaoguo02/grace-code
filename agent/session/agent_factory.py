"""Unified Agent Factory — single entry point for creating configured agents.

Eliminates the "dual pipeline" problem where SessionRuntime and ChatSession
each had their own agent creation logic. After this factory, there is ONE
path for creating a ReActAgent with proper registry wrapping, contract
resolution, and config assembly.

Architecture:
  AgentFactory.create(agent_name="plan", ...) → AgentAssembly
    ├─ spec = agent_registry.get(agent_name)
    ├─ contract = TaskContract.for_plan(cfg) | for_build(cfg)
    ├─ registry = build_registry_for_session(spec, session, ...)
    │     └─ ALWAYS wraps in PolicyAwareToolRegistry
    ├─ agent_cfg = _build_agent_config(spec, root_cfg, breaker)
    └─ agent = ReActAgent(backend, registry, agent_cfg, memory_context)

Callers (SessionRuntime, ChatSession) receive a fully configured agent
and only need to inject history, create a Task, and call agent.run().
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent.task import TaskIntent
from agent.session.models import SessionMode

if TYPE_CHECKING:
    from agent.core import AgentConfig, ReActAgent
    from agent.session.models import AgentDefinition
    from agent.session.task_contract import TaskContract
    from llm.base import LLMBackend
    from core.base import ToolRegistry


@dataclass
class AgentAssembly:
    """Fully configured agent, ready for history injection + execution."""
    agent: "ReActAgent"
    spec: "AgentDefinition"
    contract: "TaskContract"
    agent_cfg: "AgentConfig"


class AgentFactory:
    """Single entry point for creating configured ReActAgent instances.

    Replaces:
      - SessionRuntime._build_registry_for_session() + _build_agent_config()
      - ChatSession.__init__() + _rebuild_agent() inline creation
      - agent/factory.py create_agent()
    """

    @staticmethod
    def resolve_task_intent(mode: str, explicit_intent: str | None = None) -> TaskIntent:
        """Resolve legacy CLI/chat mode names to a declared task intent.

        Compatibility helper kept for tests and legacy entrypoints while the
        canonical execution path continues to derive intent from AgentDefinition.
        """
        if explicit_intent is not None:
            return TaskIntent(explicit_intent)
        mode_map = {
            "v2-build": TaskIntent.EDIT,
            "v2-plan": TaskIntent.ANALYSIS,
        }
        try:
            return mode_map[mode]
        except KeyError as exc:
            raise ValueError(f"No default task intent for mode: {mode}") from exc

    @staticmethod
    def create(
        *,
        agent_name: str,
        backend: "LLMBackend",
        base_registry: "ToolRegistry",
        agent_registry=None,
        root_agent_config: "AgentConfig",
        memory_context=None,
        session=None,
        circuit_breaker=None,
        runtime=None,
        repo_path: str | None = None,
        mcp_tool_names: frozenset[str] = frozenset(),
        session_memory_tracker=None,
    ) -> AgentAssembly:
        """Create a fully configured agent for the given agent_name.

        Args:
            agent_name: "plan", "build", etc. — resolved via agent_registry
            backend: LLM backend
            base_registry: Full tool registry (unwrapped)
            agent_registry: AgentRegistryV2 for spec + tool resolution
            root_agent_config: Root AgentConfig from CLI/chat
            memory_context: MemoryContext (only for primary agents)
            session: V2 session (for registry builder + task tool)
            circuit_breaker: Shared circuit breaker
            runtime: SessionRuntime (for task tool injection)
            mcp_tool_names: MCP tools to include

        Returns:
            AgentAssembly with agent, spec, contract, agent_cfg
        """
        from agent.session.registry_builder import build_registry_for_session
        from agent.session.task_contract import TaskContract

        # ── 0. Resolve the Runtime-owned project fact source ──
        from pathlib import Path
        _repo_source = session.repo_path if session is not None else repo_path
        if _repo_source is None:
            raise ValueError("AgentFactory.create requires an explicit repo_path")
        _project_root = str(Path(_repo_source).expanduser().resolve())

        if agent_registry is None:
            from agent.session.agent_registry import AgentRegistryV2
            agent_registry = AgentRegistryV2(project_dir=_project_root)
        elif agent_registry.project_dir != _project_root:
            raise ValueError(
                "Agent registry project scope does not match the execution repo: "
                f"registry={agent_registry.project_dir!r}, repo={_project_root!r}"
            )
        if circuit_breaker is None:
            from core.circuit_breaker import CircuitBreaker
            circuit_breaker = CircuitBreaker()

        # ── 1. Resolve agent name ──
        # Try direct registry lookup first. Legacy mode names (v2-build,
        # auto, react) are mapped as fallback for backward compatibility.
        try:
            spec = agent_registry.get(agent_name)
        except KeyError:
            _MODE_MAP = {
                "v2-build": "build", "v2-plan": "plan",
                "auto": "build", "react": "build",
            }
            _resolved = _MODE_MAP.get(agent_name)
            if _resolved is None:
                raise ValueError(
                    f"Unknown agent: {agent_name!r}. "
                    f"Available: {list(agent_registry.list_all())}"
                ) from None
            spec = agent_registry.get(_resolved)

        # Validate plan-mode agents have analysis intent + read-only scope
        if spec.permission_mode == "plan":
            from agent.session.models import DelegationScope
            if (
                spec.intent is not TaskIntent.ANALYSIS
                or spec.effective_delegation_scope is not DelegationScope.READ_ONLY
            ):
                raise ValueError(
                    "Agents with permission_mode 'plan' must declare analysis "
                    "intent and a read-only delegation scope"
                )

        contract: "TaskContract"
        if spec.intent is TaskIntent.ANALYSIS:
            contract = TaskContract.for_plan(root_agent_config)
        else:
            contract = TaskContract.for_build(root_agent_config)

        # ── 2. Build registry (always PolicyAware-wrapped) ──
        if session is not None:
            registry = build_registry_for_session(
                spec, session,
                base_registry=base_registry,
                agent_registry=agent_registry,
                circuit_breaker=circuit_breaker,
                runtime=runtime,
                mcp_tool_names=mcp_tool_names,
            )
        else:
            # No v2 session (e.g. ChatSession): wrap base registry directly
            from core.policy_registry import PolicyAwareToolRegistry
            from core.policy import PhasePolicy
            declared = agent_registry.tool_names_for(spec.name)
            filtered_registry = base_registry.filtered(declared)
            registry = PolicyAwareToolRegistry(
                base=filtered_registry,
                phase_policy=PhasePolicy(
                    allowed_tools=frozenset(filtered_registry.tool_names),
                    permission_mode=spec.permission_mode,
                ),
                repo_path=_project_root,
                phase_name="execution",
            )

        # ── 3. Build agent config ──
        agent_cfg = AgentFactory._build_agent_config(
            spec, root_agent_config, circuit_breaker,
        )

        # ── 3.5. Create TaskStateMachine — the Runtime's central authority ──
        # task_id is a placeholder; it will be updated when the actual Task
        # is created in _run_body() / SessionRuntime.run_session().
        from agent.session.task_state_machine import TaskStateMachine
        _tsm = TaskStateMachine(task_id=agent_name)

        # ── 4. Create agent (controller_factory injectable for DI) ──
        from agent.core import ReActAgent
        from agent.runtime_controller import RuntimeController
        agent = ReActAgent(
            backend, registry, agent_cfg,
            memory_context=memory_context if spec.mode == SessionMode.PRIMARY else None,
            session_memory_tracker=session_memory_tracker,
            controller_factory=RuntimeController,  # DI: swap for custom controllers
            state_machine=_tsm,
        )

        return AgentAssembly(
            agent=agent, spec=spec, contract=contract, agent_cfg=agent_cfg,
        )

    @staticmethod
    def _build_agent_config(
        spec: "AgentDefinition",
        root_cfg: "AgentConfig",
        circuit_breaker=None,
    ) -> "AgentConfig":
        """Build per-agent AgentConfig from root + spec."""
        cfg = copy.copy(root_cfg)
        cfg.circuit_breaker = circuit_breaker
        if spec.effort:
            cfg.effort = spec.effort
        if spec.mode != SessionMode.PRIMARY:
            cfg.max_steps = min(cfg.max_steps, spec.max_turns)
            cfg.is_subagent = True              # 精简 system prompt
            # CC-aligned: sub-agents can compact for long-running tasks
            cfg.stream = False
            cfg.stream_callback = None
            cfg.thought_callback = None
            cfg.token_callback = None
        return cfg
