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

if TYPE_CHECKING:
    from agent.core import AgentConfig, ReActAgent
    from agent.v2.models import AgentDefinition
    from agent.v2.task_contract import TaskContract
    from llm.base import LLMBackend
    from tools.base import ToolRegistry


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
        mcp_tool_names: frozenset[str] = frozenset(),
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
        from agent.v2.registry_builder import build_registry_for_session
        from agent.v2.task_contract import TaskContract

        # ── 0. Defaults for optional infra ──
        if agent_registry is None:
            from agent.v2.agent_registry import AgentRegistryV2
            agent_registry = AgentRegistryV2()
        if circuit_breaker is None:
            from agent.circuit_breaker import CircuitBreaker
            circuit_breaker = CircuitBreaker()

        # ── 1. Resolve mode → agent name ──
        # External modes (v2-build, v2-plan, auto, react, ...) map to
        # internal agent names (build, plan). Unknown modes default to "build".
        _MODE_MAP = {
            "v2-build": "build", "v2-plan": "plan",
            "build": "build", "plan": "plan",
            "auto": "build", "react": "build",
        }
        _resolved = _MODE_MAP.get(agent_name, "build")
        spec = agent_registry.get(_resolved)

        contract: "TaskContract"
        if _resolved == "plan":
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
            from agent.policy_registry import PolicyAwareToolRegistry
            from agent.policy import PhasePolicy
            declared = agent_registry.tool_names_for(spec.name)
            filtered_registry = base_registry.filtered(declared)
            registry = PolicyAwareToolRegistry(
                base=filtered_registry,
                phase_policy=PhasePolicy(allowed_tools=frozenset(filtered_registry.tool_names)),
                repo_path=".",
                phase_name="execution",
            )

        # ── 3. Build agent config ──
        agent_cfg = AgentFactory._build_agent_config(
            spec, root_agent_config, circuit_breaker,
        )

        # ── 4. Create agent (controller_factory injectable for DI) ──
        from agent.core import ReActAgent
        from agent.runtime_controller import RuntimeController
        agent = ReActAgent(
            backend, registry, agent_cfg,
            memory_context=memory_context if spec.mode == "primary" else None,
            controller_factory=RuntimeController,  # DI: swap for custom controllers
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
        if spec.mode != "primary":
            cfg.max_steps = min(cfg.max_steps, spec.max_turns)
            cfg.compact_history = False
            cfg.stream = False
            cfg.stream_callback = None
            cfg.thought_callback = None
        return cfg
