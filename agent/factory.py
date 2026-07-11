"""
agent/factory.py

Agent factory. Creates ReActAgent instances from mode presets.
Mode is not an if-else trigger — it selects a TaskContract preset that
determines the agent's execution boundary (tools, budget, steps).

V1 modes (plan/dag/multi-agent/auto) have been removed.
All callers route through V2 SessionRuntime or use this factory with presets.
"""

from __future__ import annotations

import re

from agent.core import AgentConfig, ReActAgent
from llm.base import LLMBackend
from tools.base import ToolRegistry


def create_agent(
    mode: str,
    backend: LLMBackend,
    registry: ToolRegistry,
    agent_config: AgentConfig | None = None,
    plan_config: object | None = None,
    task_description: str | None = None,
    plan_approval_callback=None,
    memory_context=None,
    multi_config: object | None = None,
) -> ReActAgent:
    """Create a ReActAgent. Delegates to AgentFactory (agent/v2/agent_factory.py).

    Kept for backward compatibility. New code should use AgentFactory directly.
    """
    if agent_config is None:
        agent_config = AgentConfig()

    from agent.v2.agent_factory import AgentFactory
    assembly = AgentFactory.create(
        agent_name=mode,
        backend=backend,
        base_registry=registry,
        root_agent_config=agent_config,
        memory_context=memory_context,
    )
    return assembly.agent


# ---------------------------------------------------------------------------
# Task intent classification
# ---------------------------------------------------------------------------

_EDIT_INDICATORS = re.compile(
    r"\b(fix|write|create|modify|change|update|add|remove|delete|"
    r"refactor|implement|rename|move|replace|install|upgrade|patch|"
    r"rewrite|migrate|编辑|修改|创建|删除|重构|添加|修复|写入)\b",
    re.IGNORECASE,
)


def classify_task_intent(
    description: str,
    intent_override: str = "auto",
    backend: object = None,
) -> str:
    """Determine task intent: "edit" or "analysis"."""
    if intent_override != "auto":
        return intent_override

    if _EDIT_INDICATORS.search(description):
        return "edit"
    return "analysis"
