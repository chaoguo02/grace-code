"""
agent/factory.py

Agent 工厂。根据 mode 创建对应的 Agent 实例。

所有入口（CLI run、Chat、GitHub Issue）通过这个工厂获取 agent 实例，
切换模式只需要改 mode 参数。

模式：
- "react" — ReActAgent，标准的 Reasoning + Acting 循环
- "plan" — PlanExecuteAgent，先规划 JSON 计划再逐步执行
- "dag" — DAGPlanExecutor，DAG 结构化计划 + 拓扑层级执行
- "multi-agent" — MultiAgentExecutor，多角色协作流水线
- "auto" — 根据任务描述自动判断：简单任务用 react，复杂任务用 plan
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from agent.core import AgentConfig, ReActAgent, PlanExecuteAgent
from agent.dag import DAGPlanExecutor
from agent.multi_agent import CoordinatorAgent, MultiAgentConfig
from agent.plan import PlanExecuteConfig
from llm.base import LLMBackend
from tools.base import ToolRegistry

if TYPE_CHECKING:
    from memory.context import MemoryContext

AgentType = ReActAgent | PlanExecuteAgent | DAGPlanExecutor | CoordinatorAgent


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
    memory_context: "MemoryContext | None" = None,
    multi_config: MultiAgentConfig | None = None,
) -> AgentType:
    """
    根据 mode 创建对应的 Agent 实例。

    Args:
        mode:             "react" | "plan" | "dag" | "auto"
        backend:          LLM 后端
        registry:         工具注册表
        agent_config:     Agent 配置（ReActAgent 和 PlanExecuteAgent 共用）
        plan_config:      PlanExecuteAgent 专用配置（mode="plan"/"dag" 时生效）
        task_description: 任务描述（mode="auto" 时用于判断复杂度）
        plan_approval_callback: Callable[[str], bool] 用户审批 plan 的回调
        memory_context:   长期记忆上下文（可选）

    Returns:
        ReActAgent / PlanExecuteAgent / DAGPlanExecutor
    """
    mode = _resolve_mode(mode, task_description)

    if mode == "multi-agent":
        if multi_config is None:
            multi_config = MultiAgentConfig()
        return CoordinatorAgent(
            backend, registry, agent_config, multi_config,
            memory_context=memory_context,
        )
    if mode == "dag":
        if plan_config is None:
            plan_config = PlanExecuteConfig()
        if plan_approval_callback:
            plan_config.plan_approval_callback = plan_approval_callback
        return DAGPlanExecutor(backend, registry, agent_config, plan_config, memory_context=memory_context)
    if mode == "plan":
        if plan_config is None:
            plan_config = PlanExecuteConfig()
        if plan_approval_callback:
            plan_config.plan_approval_callback = plan_approval_callback
        return PlanExecuteAgent(backend, registry, agent_config, plan_config)
    return ReActAgent(backend, registry, agent_config, memory_context=memory_context)


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
    if mode not in ("react", "plan", "dag", "multi-agent"):
        raise ValueError(f"Unknown mode: {mode!r}, expected 'react', 'plan', 'dag', 'multi-agent', or 'auto'")
    return mode


# ---------------------------------------------------------------------------
# 任务意图分类
# ---------------------------------------------------------------------------

_EDIT_SIGNALS = re.compile(
    r"(fix|bug|error|implement|feature|add|create|write|modify|change|update|refactor|"
    r"rewrite|delete|remove|rename|move|replace|"
    r"修复|修改|添加|新增|增加|插入|补充|实现|重构|删除|移动|替换|创建|写入|改写|更新)",
    re.IGNORECASE,
)

_ANALYSIS_SIGNALS = re.compile(
    r"(什么|哪些|如何|为什么|多少|几个|是否|有没有|"
    r"explain|what|which|how|why|where|who|when|"
    r"list|describe|tell\s*me|show\s*me|count|"
    r"分析|解释|列出|说明|总结|描述|查看|读取|了解|搜索|找到|找出|"
    r"summarize|analyze|find.*and\s*(tell|explain|describe|list)|search|look\s*for|find)",
    re.IGNORECASE,
)


def classify_task_intent(description: str) -> str:
    """
    根据任务描述判断意图。

    Returns:
        "edit"     — 需要修改文件的任务
        "analysis" — 纯阅读/分析/问答类任务
    """
    text = description.strip()
    if _EDIT_SIGNALS.search(text):
        return "edit"
    if _ANALYSIS_SIGNALS.search(text):
        return "analysis"
    return "edit"
