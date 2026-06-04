"""
agent/plan.py

Plan-and-Execute 编排层的数据结构。

Plan / SubTask 是 PlanExecuteAgent 使用的高层抽象，
描述"要执行什么子任务"和"按什么顺序执行"。

不是 ReAct 层的一部分——ReActAgent 不需要知道 Plan 的存在。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class PlanExecuteConfig:
    """PlanExecuteAgent 专用配置。"""
    plan_max_subtasks: int = 10
    plan_subtask_log_dir: str = "./logs/subtasks"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

class PlanGenerationError(Exception):
    """Plan 生成或解析失败时抛出。调用方应降级到 ReActAgent。"""
    pass


@dataclass
class SubTask:
    """执行计划中的一个子任务。"""
    id: str                         # "1", "2", ...
    description: str                # 传给 ReActAgent 的 Task.description
    expected_outcome: str = ""      # 预期结果（供 plan 上下文使用）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __repr__(self) -> str:
        return f"SubTask(id={self.id!r}, desc={self.description[:40]!r})"


@dataclass
class Plan:
    """LLM 生成的执行计划。"""
    original_task: str              # 原始任务描述
    subtasks: list[SubTask]         # 子任务列表
    reasoning: str = ""             # 规划的理由/思考

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_task": self.original_task,
            "reasoning": self.reasoning,
            "subtasks": [s.to_dict() for s in self.subtasks],
        }

    @classmethod
    def from_json(cls, json_str: str, original_task: str) -> "Plan":
        """
        从 LLM 输出的 JSON 文本解析 Plan。

        容忍以下情况：
        - 被 markdown 代码块包裹（```json ... ```）
        - 前缀/后缀有无关文本（裁取第一个 { 到最后一个 } 之间）

        Raises:
            PlanGenerationError: JSON 无效或缺少必要字段
        """
        text = json_str.strip()

        # 去除 markdown 代码块
        if text.startswith("```"):
            first_nl = text.find("\n")
            if first_nl != -1:
                text = text[first_nl + 1:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        # 裁取 JSON 对象
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start == -1 or brace_end == -1 or brace_end <= brace_start:
            raise PlanGenerationError(
                "No JSON object found in plan response"
            )
        text = text[brace_start:brace_end + 1]

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PlanGenerationError(f"Invalid JSON in plan: {exc}") from exc

        if not isinstance(data, dict):
            raise PlanGenerationError("Plan JSON must be a dictionary")

        if "plan" not in data:
            raise PlanGenerationError("Plan JSON missing 'plan' key")

        raw_plan = data["plan"]
        if not isinstance(raw_plan, list) or len(raw_plan) == 0:
            raise PlanGenerationError(
                "Plan must contain at least one subtask"
            )

        subtasks: list[SubTask] = []
        for entry in raw_plan:
            if not isinstance(entry, dict):
                raise PlanGenerationError(
                    f"Invalid subtask entry: {entry!r}"
                )
            if "id" not in entry or "description" not in entry:
                raise PlanGenerationError(
                    f"Subtask missing 'id' or 'description': {entry!r}"
                )
            subtasks.append(SubTask(
                id=str(entry["id"]),
                description=entry["description"],
                expected_outcome=entry.get("expected_outcome", ""),
            ))

        return cls(
            original_task=original_task,
            subtasks=subtasks,
            reasoning=data.get("reasoning", ""),
        )

    def __repr__(self) -> str:
        return (
            f"Plan(subtasks={len(self.subtasks)}, "
            f"reasoning={self.reasoning[:50]!r})"
        )
