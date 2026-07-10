"""
agent/factory.py

Agent 工厂。根据 mode 创建对应的 Agent 实例。
V1 模式已移除 —— 所有入口统一走 V2 SessionRuntime。
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
    """Create a ReActAgent instance.

    All V1 modes (plan / dag / multi-agent / auto) have been removed.
    Every caller now routes through V2 SessionRuntime.
    """
    if agent_config is None:
        agent_config = AgentConfig()
    return ReActAgent(backend, registry, agent_config, memory_context=memory_context)


# ---------------------------------------------------------------------------
# 任务意图分类
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
