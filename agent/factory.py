"""Agent construction and explicit mode-to-intent declarations."""

from __future__ import annotations

from agent.core import AgentConfig, ReActAgent
from agent.task import TaskIntent
from llm.base import LLMBackend
from tools.base import ToolRegistry


def create_agent(
    mode: str,
    backend: LLMBackend,
    registry: ToolRegistry,
    agent_config: AgentConfig | None = None,
    plan_config: object | None = None,
    task_description: str | None = None,
    memory_context=None,
    multi_config: object | None = None,
    repo_path: str | None = None,
) -> ReActAgent:
    """Create a ReActAgent through the V2 declarative agent factory."""
    del plan_config, task_description, multi_config
    if agent_config is None:
        agent_config = AgentConfig()

    from agent.v2.agent_factory import AgentFactory

    assembly = AgentFactory.create(
        agent_name=mode,
        backend=backend,
        base_registry=registry,
        root_agent_config=agent_config,
        memory_context=memory_context,
        repo_path=repo_path,
    )
    return assembly.agent


# Deprecated: prefer AgentDefinition.intent directly.
# The caller should look up the agent definition by name and read definition.intent.
_DEFAULT_INTENT_BY_MODE = {
    "v2-build": TaskIntent.EDIT,
    "build": TaskIntent.EDIT,
    "v2-plan": TaskIntent.ANALYSIS,
    "plan": TaskIntent.ANALYSIS,
}


def resolve_task_intent(
    mode: str,
    intent_override: TaskIntent | str | None = None,
) -> TaskIntent:
    """Deprecated: look up AgentDefinition.intent instead.

    Kept for backward compat with tests and callers that haven't migrated yet.
    """
    if intent_override is not None:
        return TaskIntent(intent_override)
    try:
        return _DEFAULT_INTENT_BY_MODE[mode]
    except KeyError as exc:
        raise ValueError(f"No default task intent declared for mode {mode!r}") from exc
