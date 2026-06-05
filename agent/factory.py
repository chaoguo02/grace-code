"""
agent/factory.py

Agent 工厂。根据 mode 创建 ReActAgent 或 PlanExecuteAgent。

所有入口（CLI run、Chat、GitHub Issue）通过这个工厂获取 agent 实例，
切换模式只需要改 mode 参数。

模式：
- "react" — ReActAgent，标准的 Reasoning + Acting 循环
- "plan" — PlanExecuteAgent，先规划 JSON 计划再逐步执行
- "auto" — 根据任务描述自动判断：简单任务用 react，复杂任务用 plan
"""

from __future__ import annotations

import re

from agent.core import AgentConfig, ReActAgent, PlanExecuteAgent
from agent.plan import PlanExecuteConfig
from llm.base import LLMBackend
from tools.base import ToolRegistry

AgentType = ReActAgent | PlanExecuteAgent


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------

def create_agent(
    mode: str,
    backend: LLMBackend,
    registry: ToolRegistry,
    agent_config: AgentConfig | None = None,
    plan_config: PlanExecuteConfig | None = None,
    task_description: str | None = None,
    plan_approval_callback=None,
) -> AgentType:
    """
    根据 mode 创建对应的 Agent 实例。

    Args:
        mode:             "react" | "plan" | "auto"
        backend:          LLM 后端
        registry:         工具注册表
        agent_config:     Agent 配置（ReActAgent 和 PlanExecuteAgent 共用）
        plan_config:      PlanExecuteAgent 专用配置（mode="plan" 时生效）
        task_description: 任务描述（mode="auto" 时用于判断复杂度）
        plan_approval_callback: Callable[[str], bool] 用户审批 plan 的回调

    Returns:
        ReActAgent 或 PlanExecuteAgent，两者都有 run(task, log) -> RunResult 接口
    """
    mode = _resolve_mode(mode, task_description)

    if mode == "plan":
        if plan_config is None:
            plan_config = PlanExecuteConfig()
        if plan_approval_callback:
            plan_config.plan_approval_callback = plan_approval_callback
        return PlanExecuteAgent(backend, registry, agent_config, plan_config)
    return ReActAgent(backend, registry, agent_config)


# ---------------------------------------------------------------------------
# 内部：auto 模式判断
# ---------------------------------------------------------------------------

# 判断为"复杂任务"的特征：
# - 多个步骤编号（"1. xxx 2. xxx"）
# - 多个动词串联（and / then / after / before）
# - 多个文件操作（"修改 A 和 B"、"重构 X"）
# - 描述长度超过阈值

_COMPLEXITY_PATTERNS = [
    re.compile(r"\d+[\.\)、]\s*\S", re.IGNORECASE),   # 步骤编号
    re.compile(r"(first|then|after|before|next|finally|step\s*\d)", re.IGNORECASE),  # 顺序词
    re.compile(r"(and|,)\s+\w+\s+(and|,)", re.IGNORECASE),  # 多个对象
    re.compile(r"(重构|refactor|rewrite|migrate|redesign)", re.IGNORECASE),  # 重量级操作
]


def _is_complex_task(description: str) -> bool:
    """启发式判断任务复杂度。"""
    text = description.strip()
    if len(text) > 300:
        return True
    if len(text.splitlines()) >= 5:
        return True
    return sum(1 for pat in _COMPLEXITY_PATTERNS if pat.search(text)) >= 1


def _resolve_mode(mode: str, task_description: str | None) -> str:
    """Resolve "auto" mode to a concrete mode."""
    if mode == "auto":
        if task_description and _is_complex_task(task_description):
            return "plan"
        return "react"
    if mode not in ("react", "plan"):
        raise ValueError(f"Unknown mode: {mode!r}, expected 'react', 'plan', or 'auto'")
    return mode
