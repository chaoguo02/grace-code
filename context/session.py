"""
context/session.py

会话状态与任务生命周期模型。

核心数据结构：
- TaskContext: 当前正在执行的任务的高保真工作上下文
- TaskSummary: 已完成任务的结构化蒸馏摘要
- SessionState: 整个 chat session 的结构化状态

设计原则：
- 当前任务内保持高保真（保留工具输出细节）
- 任务边界时蒸馏为 TaskSummary（保留决策和结果，丢弃过程细节）
- 滚动 session summary 从 TaskSummary 列表构建
- 与 context/stats.py 配合提供可观测性
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from context.stats import ContextStats
from context.token_budget import estimate_tokens
from agent.task import TaskIntent


# ---------------------------------------------------------------------------
# TaskContext — 当前任务的工作上下文
# ---------------------------------------------------------------------------

@dataclass
class TaskContext:
    """当前正在执行任务的高保真上下文。"""
    task_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    user_goal: str = ""
    intent: TaskIntent = TaskIntent.EDIT
    started_at: float = field(default_factory=time.time)
    active_files: set[str] = field(default_factory=set)
    decisions: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# TaskSummary — 已完成任务的结构化蒸馏
# ---------------------------------------------------------------------------

@dataclass
class TaskSummary:
    """已完成任务的蒸馏摘要，不含原始工具输出。"""
    task_id: str = ""
    user_goal: str = ""
    outcome: str = ""
    changed_files: list[str] = field(default_factory=list)
    read_files: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    steps_taken: int = 0
    tokens_spent: int = 0
    elapsed_seconds: float = 0.0

    def to_text(self) -> str:
        """生成面向 prompt 注入的紧凑文本表示。"""
        lines = [f"Task: {self.user_goal}"]
        lines.append(f"Outcome: {self.outcome}")
        if self.changed_files:
            lines.append(f"Changed: {', '.join(self.changed_files[:10])}")
        if self.commands:
            lines.append(f"Commands: {', '.join(self.commands[:5])}")
        if self.tests:
            lines.append(f"Tests: {', '.join(self.tests[:5])}")
        if self.decisions:
            lines.append(f"Decisions: {'; '.join(self.decisions[:5])}")
        if self.unresolved:
            lines.append(f"Unresolved: {'; '.join(self.unresolved[:3])}")
        return "\n".join(lines)

    def estimated_tokens(self) -> int:
        return estimate_tokens(self.to_text())


# ---------------------------------------------------------------------------
# SessionState — 整个 chat session 的结构化状态
# ---------------------------------------------------------------------------

@dataclass
class SessionState:
    """Chat session 的结构化状态。替代简单的 _shared_history。"""
    session_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    round_count: int = 0
    active_task: TaskContext | None = None
    completed_tasks: list[TaskSummary] = field(default_factory=list)
    rolling_summary: str = ""
    compaction_count: int = 0
    last_compaction_reason: str | None = None

    def start_task(
        self,
        user_goal: str,
        intent: TaskIntent | str = TaskIntent.EDIT,
    ) -> TaskContext:
        """开始一个新任务，返回 TaskContext。"""
        self.round_count += 1
        ctx = TaskContext(
            user_goal=user_goal,
            intent=TaskIntent(intent),
        )
        self.active_task = ctx
        return ctx

    def finish_task(self, summary: TaskSummary) -> None:
        """结束当前任务，将 summary 加入已完成列表。"""
        self.completed_tasks.append(summary)
        self.active_task = None
        self._update_rolling_summary()

    def _update_rolling_summary(self) -> None:
        """从已完成任务列表构建滚动摘要文本。"""
        if not self.completed_tasks:
            self.rolling_summary = ""
            return
        parts = []
        for i, ts in enumerate(self.completed_tasks[-5:], 1):
            parts.append(f"[Round {i}] {ts.to_text()}")
        self.rolling_summary = "\n\n".join(parts)

    def get_session_context_for_prompt(self, budget_tokens: int = 12000) -> str:
        """获取适合注入 prompt 的 session 上下文。尊重 token 预算。"""
        if not self.rolling_summary:
            return ""
        if estimate_tokens(self.rolling_summary) <= budget_tokens:
            return self.rolling_summary
        # 预算不足：只保留最近几个任务
        parts = []
        for ts in reversed(self.completed_tasks):
            text = ts.to_text()
            parts.insert(0, text)
            combined = "\n\n".join(parts)
            if estimate_tokens(combined) > budget_tokens:
                parts.pop(0)
                break
        return "\n\n".join(parts)

    def estimated_tokens(self) -> int:
        """估算当前 session state 的总 token 数。"""
        return estimate_tokens(self.rolling_summary) if self.rolling_summary else 0
