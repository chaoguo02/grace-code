"""
agent/plan.py

Plan-and-Execute 编排层的数据结构。

新设计（Claude Code 风格）：
- Plan 现在支持 markdown 文本格式（人类可读的策略文档）
- Phase 1（只读探索）→ Phase 2（执行），中间需要用户审批
- 旧的 JSON subtask 格式保留向后兼容

Plan / SubTask 是 PlanExecuteAgent 使用的高层抽象。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class PlanApproval:
    """用户对计划的审批结果。"""
    approved: bool
    action: str = "execute"  # execute | revise
    feedback: str = ""


@dataclass
class PlanExecuteConfig:
    """PlanExecuteAgent 专用配置。"""
    plan_max_subtasks: int = 10
    plan_subtask_log_dir: str = "./logs/subtasks"
    plan_approval_callback: Any = None  # Callable[[str], bool | PlanApproval] — 用户审批回调
    enable_replan: bool = False
    max_replans: int = 1
    allow_parallel_verification: bool = False
    allow_parallel_commands: bool = False


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

class PlanGenerationError(Exception):
    """Plan 生成或解析失败时抛出。调用方应降级到 ReActAgent。"""
    pass


class SubTaskType(str, Enum):
    """DAG subtask 的语义类型，用于展示、统计和后续调度策略。"""
    PLANNING = "planning"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    COMMAND = "command"
    ANALYSIS = "analysis"
    VERIFICATION = "verification"

    @classmethod
    def coerce(cls, value: Any) -> "SubTaskType":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).lower())
        except (ValueError, TypeError):
            return cls.ANALYSIS


class SubTaskStatus(str, Enum):
    """DAG subtask 生命周期状态。"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"

    @classmethod
    def coerce(cls, value: Any) -> "SubTaskStatus":
        if isinstance(value, cls):
            return value
        text = str(value).lower()
        if text == "done":
            return cls.COMPLETED
        try:
            return cls(text)
        except ValueError:
            return cls.PENDING


@dataclass
class SubTask:
    """执行计划中的一个子任务。"""
    id: str                         # "1", "2", ...
    original_id: str = ""           # LLM 原始 id（规范化前）
    description: str = ""           # 传给 ReActAgent 的 Task.description
    expected_outcome: str = ""      # 预期结果（供 plan 上下文使用）
    result_summary: str = ""        # 执行后的结果摘要（跨 subtask 传递）
    depends_on: list[str] = field(default_factory=list)   # DAG 依赖的上游 subtask id 列表
    type: SubTaskType = SubTaskType.ANALYSIS
    status: SubTaskStatus = SubTaskStatus.PENDING
    error: str = ""
    skip_reason: str = ""
    dependents: list[str] = field(default_factory=list)
    start_time_ms: int = 0
    end_time_ms: int = 0
    duration_ms: float = 0.0

    def __post_init__(self) -> None:
        self.type = SubTaskType.coerce(self.type)
        self.status = SubTaskStatus.coerce(self.status)
        self.depends_on = [str(dep) for dep in self.depends_on]
        self.dependents = [str(dep) for dep in self.dependents]

    def mark_started(self) -> None:
        self.status = SubTaskStatus.RUNNING
        self.start_time_ms = _now_ms()
        self.end_time_ms = 0
        self.duration_ms = 0.0
        self.error = ""
        self.skip_reason = ""

    def mark_completed(self, result: str) -> None:
        self.status = SubTaskStatus.COMPLETED
        self.result_summary = result
        self.error = ""
        self.skip_reason = ""
        self._mark_finished()

    def mark_failed(self, error: str) -> None:
        self.status = SubTaskStatus.FAILED
        self.error = error
        self.result_summary = error
        self.skip_reason = ""
        self._mark_finished()

    def mark_skipped(self, reason: str) -> None:
        self.status = SubTaskStatus.SKIPPED
        self.skip_reason = reason
        self.error = ""
        self.result_summary = reason
        self._mark_finished()

    def _mark_finished(self) -> None:
        if not self.start_time_ms:
            self.start_time_ms = _now_ms()
        self.end_time_ms = _now_ms()
        self.duration_ms = max(0.0, float(self.end_time_ms - self.start_time_ms))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        d["status"] = self.status.value
        if not d["depends_on"]:
            del d["depends_on"]
        if not d["dependents"]:
            del d["dependents"]
        if d["type"] == SubTaskType.ANALYSIS.value:
            del d["type"]
        if d["status"] == SubTaskStatus.PENDING.value:
            del d["status"]
        for key in ("original_id", "result_summary", "error", "skip_reason"):
            if not d[key]:
                del d[key]
        for key in ("start_time_ms", "end_time_ms", "duration_ms"):
            if not d[key]:
                del d[key]
        return d

    def __repr__(self) -> str:
        return f"SubTask(id={self.id!r}, type={self.type.value!r}, desc={self.description[:40]!r})"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _extract_json_object(json_text: str, label: str = "plan") -> dict[str, Any]:
    text = json_text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start == -1 or brace_end == -1 or brace_end <= brace_start:
        raise PlanGenerationError(f"No JSON object found in {label} response")

    try:
        data = json.loads(text[brace_start:brace_end + 1])
    except json.JSONDecodeError as exc:
        raise PlanGenerationError(f"Invalid JSON in {label}: {exc}") from exc

    if not isinstance(data, dict):
        raise PlanGenerationError(f"{label} JSON must be a dictionary")
    return data


def _extract_plan_items(data: dict[str, Any], label: str = "plan") -> list[dict[str, Any]]:
    raw_plan = data.get("plan", data.get("tasks"))
    if not isinstance(raw_plan, list) or len(raw_plan) == 0:
        raise PlanGenerationError(f"{label} must contain at least one subtask")
    if not all(isinstance(entry, dict) for entry in raw_plan):
        raise PlanGenerationError(f"{label} contains invalid subtask entries")
    return raw_plan


def _extract_dependencies(entry: dict[str, Any]) -> list[str]:
    deps = entry.get("depends_on", entry.get("dependencies", []))
    if not isinstance(deps, list):
        return []
    return [str(dep) for dep in deps]


def _extract_reasoning(data: dict[str, Any]) -> str:
    return str(data.get("reasoning") or data.get("summary") or "")


def _extract_summary(data: dict[str, Any]) -> str:
    return str(data.get("summary") or "")


def _normalize_subtask_entries(
    entries: list[dict[str, Any]],
    include_dependencies: bool,
) -> list[SubTask]:
    id_mapping: dict[str, str] = {}
    for index, entry in enumerate(entries, start=1):
        original_id = str(entry.get("id") or f"task_{index}")
        # 重复 ID 时保留第一次映射，避免后续重复项把依赖重定向到自身。
        id_mapping.setdefault(original_id, f"task_{index}")

    subtasks: list[SubTask] = []
    for index, entry in enumerate(entries, start=1):
        if "description" not in entry:
            raise PlanGenerationError(f"Subtask missing 'description': {entry!r}")
        original_id = str(entry.get("id") or f"task_{index}")
        depends_on = []
        if include_dependencies:
            depends_on = [id_mapping.get(dep, dep) for dep in _extract_dependencies(entry)]
        subtasks.append(SubTask(
            id=f"task_{index}",
            original_id=original_id,
            description=str(entry["description"]),
            expected_outcome=str(entry.get("expected_outcome", "")),
            depends_on=depends_on,
            type=SubTaskType.coerce(entry.get("type", SubTaskType.ANALYSIS.value)),
        ))
    return subtasks


@dataclass
class Plan:
    """LLM 生成的执行计划。"""
    original_task: str              # 原始任务描述
    subtasks: list[SubTask]         # 子任务列表
    reasoning: str = ""             # 规划的理由/思考
    summary: str = ""               # 面向用户/日志的计划摘要

    def to_dict(self) -> dict[str, Any]:
        data = {
            "original_task": self.original_task,
            "reasoning": self.reasoning,
            "subtasks": [s.to_dict() for s in self.subtasks],
        }
        if self.summary:
            data["summary"] = self.summary
        return data

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
        data = _extract_json_object(json_str, label="plan")
        entries = _extract_plan_items(data, label="plan")
        subtasks = _normalize_subtask_entries(entries, include_dependencies=False)

        return cls(
            original_task=original_task,
            subtasks=subtasks,
            reasoning=_extract_reasoning(data),
            summary=_extract_summary(data),
        )

    @classmethod
    def from_markdown(cls, markdown_text: str, original_task: str) -> "Plan":
        """
        从 markdown 文本创建 Plan（Claude Code 风格）。

        新模式下，plan 就是人类可读的 markdown 策略文档，
        不需要解析为结构化 subtask。
        """
        return cls(
            original_task=original_task,
            subtasks=[],
            reasoning=markdown_text,
        )

    @classmethod
    def from_dag_json(cls, json_str: str, original_task: str) -> "Plan":
        """
        从带 depends_on 的 JSON 解析 DAG Plan。

        期望格式:
        {
          "reasoning": "...",
          "plan": [
            {"id": "1", "description": "...", "depends_on": []},
            {"id": "2", "description": "...", "depends_on": ["1"]},
          ]
        }

        Raises:
            PlanGenerationError: JSON 无效或缺少必要字段
        """
        data = _extract_json_object(json_str, label="DAG plan")
        entries = _extract_plan_items(data, label="DAG plan")
        subtasks = _normalize_subtask_entries(entries, include_dependencies=True)

        return cls(
            original_task=original_task,
            subtasks=subtasks,
            reasoning=_extract_reasoning(data),
            summary=_extract_summary(data),
        )

    @property
    def is_markdown_plan(self) -> bool:
        """判断是否为新式 markdown plan（无 subtask）。"""
        return len(self.subtasks) == 0 and bool(self.reasoning)

    @property
    def is_dag_plan(self) -> bool:
        """判断是否为 DAG plan（subtask 含 depends_on）。"""
        return len(self.subtasks) > 0 and any(
            len(st.depends_on) > 0 for st in self.subtasks
        )

    @property
    def plan_text(self) -> str:
        """获取 plan 文本（markdown plan 用 reasoning 字段存储）。"""
        if self.is_markdown_plan:
            return self.reasoning
        parts = [f"Reasoning: {self.reasoning}\n"]
        for st in self.subtasks:
            parts.append(f"  {st.id}. {st.description}")
            if st.expected_outcome:
                parts.append(f"     Expected: {st.expected_outcome}")
        return "\n".join(parts)

    def __repr__(self) -> str:
        if self.is_markdown_plan:
            return f"Plan(markdown, len={len(self.reasoning)})"
        return (
            f"Plan(subtasks={len(self.subtasks)}, "
            f"reasoning={self.reasoning[:50]!r})"
        )
