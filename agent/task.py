"""
agent/task.py

整个项目的数据地基。定义 agent 运行过程中所有核心概念：
Task / ToolCall / Action / Observation / Event / RunResult

设计原则：
- 全部使用 dataclass，类型安全，IDE 友好
- 每个类都可以 asdict() 序列化为 JSON，供 EventLog 使用
- 枚举类型用 str enum，序列化后人类可读
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    """Event log 中的事件类型，str enum 序列化后直接是字符串。"""
    TASK_START       = "task_start"
    ACTION           = "action"
    OBSERVATION      = "observation"
    REFLECTION       = "reflection"
    PLAN_GENERATED   = "plan_generated"
    SUBTASK_START    = "subtask_start"
    SUBTASK_COMPLETE = "subtask_complete"
    SUBTASK_FAILED   = "subtask_failed"
    TASK_COMPLETE    = "task_complete"
    TASK_FAILED      = "task_failed"


class ActionType(str, Enum):
    """Agent 可以执行的 action 类型。"""
    TOOL_CALL   = "tool_call"    # 调用某个工具
    REFLECTION  = "reflection"   # 触发自我反思
    FINISH      = "finish"       # 宣布任务完成
    GIVE_UP     = "give_up"      # 超出能力范围，主动放弃


class ObservationStatus(str, Enum):
    """工具执行结果的状态。"""
    SUCCESS = "success"
    ERROR   = "error"
    TIMEOUT = "timeout"


class RunStatus(str, Enum):
    """整次 agent 运行的最终状态。"""
    SUCCESS     = "success"
    FAILED      = "failed"
    MAX_STEPS   = "max_steps"    # 达到步数上限
    GAVE_UP     = "gave_up"      # agent 主动放弃


# ---------------------------------------------------------------------------
# Task — 输入
# ---------------------------------------------------------------------------

@dataclass
class Task:
    """
    一次 agent 运行的输入描述。
    由 entry 层（CLI / GitHub Issue）构造，传给 Agent.run()。
    """
    # 必填
    description: str            # 任务描述，自然语言
    repo_path: str              # 目标 repo 的本地路径

    # 可选
    task_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    intent: str = "edit"                # 任务意图: "edit" (编辑型) | "analysis" (分析型)
    issue_url: str | None = None        # GitHub issue URL，自动修复模式时填入
    test_cmd: str | None = None         # 运行测试的命令，如 "pytest tests/"
    max_steps: int = 40                 # 最大循环步数，超出则熔断
    budget_tokens: int = 80_000         # 整次运行的 token 预算

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __repr__(self) -> str:
        return f"Task(id={self.task_id!r}, desc={self.description[:60]!r})"


# ---------------------------------------------------------------------------
# ToolCall — Action 的载体
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    """
    agent 决定调用某个工具时的具体参数。
    嵌套在 Action 里，不单独写入 EventLog。
    """
    name: str                   # 工具名称，如 "shell", "file_read"
    params: dict[str, Any]      # 工具参数，由各 Tool 自己定义 schema
    id: str | None = None       # Provider-assigned tool call ID (for native tool_use round-trip)

    def to_dict(self) -> dict[str, Any]:
        d = {"name": self.name, "params": self.params}
        if self.id is not None:
            d["id"] = self.id
        return d


# ---------------------------------------------------------------------------
# Action — agent 的决策输出
# ---------------------------------------------------------------------------

@dataclass
class Action:
    """
    LLM 每轮输出的决策。
    由 LLMBackend 解析返回，Agent Core 拿到后执行。
    """
    action_type: ActionType
    thought: str                            # LLM 的推理过程，必须保留，调试关键
    tool_calls: list[ToolCall] = field(default_factory=list)  # action_type == TOOL_CALL 时一个或多个
    message: str | None = None             # action_type == FINISH / GIVE_UP 时的说明

    def to_dict(self) -> dict[str, Any]:
        d = {
            "action_type": self.action_type.value,
            "thought": self.thought,
            "message": self.message,
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
        }
        return d

    def is_terminal(self) -> bool:
        """是否是终止 action（不再需要执行工具）。"""
        return self.action_type in (ActionType.FINISH, ActionType.GIVE_UP)

    def __repr__(self) -> str:
        if self.tool_calls:
            names = " + ".join(tc.name for tc in self.tool_calls)
            return f"Action({self.action_type.value}, tools=[{names}])"
        return f"Action({self.action_type.value})"


# ---------------------------------------------------------------------------
# Observation — 工具执行结果
# ---------------------------------------------------------------------------

@dataclass
class Observation:
    """
    工具执行后返回给 agent 的结果。
    output 字段会被注入下一轮 LLM 的上下文。
    """
    status: ObservationStatus
    output: str                         # 工具输出，已截断到安全长度
    tool_name: str                      # 来自哪个工具
    tokens_used: int = 0                # 这条 observation 消耗的 token 数（估算）
    error: str | None = None            # status == ERROR 时的错误信息

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def is_success(self) -> bool:
        return self.status == ObservationStatus.SUCCESS

    def __repr__(self) -> str:
        return (
            f"Observation(tool={self.tool_name}, "
            f"status={self.status.value}, "
            f"len={len(self.output)})"
        )


# ---------------------------------------------------------------------------
# Event — 写入 EventLog 的统一单元
# ---------------------------------------------------------------------------

@dataclass
class Event:
    """
    EventLog 中的一条记录。
    所有信息统一封装在这里，append-only 写入 JSONL 文件。

    payload 的内容取决于 event_type：
    - TASK_START:    {"task": Task.to_dict()}
    - ACTION:        {"step": int, "action": Action.to_dict()}
    - OBSERVATION:   {"step": int, "observation": Observation.to_dict()}
    - REFLECTION:    {"step": int, "reason": str, "prompt": str}
    - TASK_COMPLETE: {"steps": int, "summary": str}
    - TASK_FAILED:   {"steps": int, "reason": str}
    """
    event_type: EventType
    task_id: str
    payload: dict[str, Any]
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    event_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id":   self.event_id,
            "event_type": self.event_type.value,
            "task_id":    self.task_id,
            "timestamp":  self.timestamp,
            "payload":    self.payload,
        }


# ---------------------------------------------------------------------------
# RunResult — 整次运行的最终结果
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    """
    Agent.run() 的返回值。
    包含最终状态、patch 内容（如有）、统计信息。
    """
    task_id: str
    status: RunStatus
    summary: str                        # 人类可读的结果摘要
    steps_taken: int
    total_tokens: int = 0
    patch: str | None = None            # git diff 格式的修改内容
    error: str | None = None            # status == FAILED 时的原因
    cache_stats: Any = None             # CacheStats 累计（可选）

    def is_success(self) -> bool:
        return self.status == RunStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __repr__(self) -> str:
        return (
            f"RunResult(status={self.status.value}, "
            f"steps={self.steps_taken}, "
            f"tokens={self.total_tokens})"
        )
