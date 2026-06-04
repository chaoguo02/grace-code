"""
agent/event_log.py

Append-only JSONL 事件日志。
整个 agent 运行过程的完整记录，支持：
- 实时写入（每条 event 立刻 flush 到磁盘）
- 确定性回放（replay 还原完整事件序列）
- 按 task_id 隔离（每次运行一个独立文件）
- 人类可读（JSONL 格式，可直接 cat / tail -f）

设计原则：
- 只增不改，写入后永不修改历史记录
- 每条写入后立即 flush，崩溃不丢最近事件
- 文件命名带 timestamp，多次运行不覆盖
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from agent.task import Event, EventType, Task, Action, Observation, RunResult
from agent.plan import Plan, SubTask


# ---------------------------------------------------------------------------
# EventLog
# ---------------------------------------------------------------------------

class EventLog:
    """
    JSONL 格式的 append-only 事件日志。

    用法：
        log = EventLog.create(task, log_dir="./logs")
        log.log_task_start(task)
        log.log_action(step=1, action=action)
        log.log_observation(step=1, observation=obs)
        log.close()

    文件路径格式：
        {log_dir}/{task_id}_{timestamp}.jsonl
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._file = open(path, "a", encoding="utf-8")  # append mode

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------

    @classmethod
    def create(cls, task: Task, log_dir: str = "./logs") -> "EventLog":
        """
        为一次新运行创建 EventLog。
        目录不存在时自动创建。
        """
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{task.task_id}_{timestamp}.jsonl"
        return cls(log_path / filename)

    @classmethod
    def open_existing(cls, path: str | Path) -> "EventLog":
        """打开已有的 EventLog 文件（用于追加写入，如断点续跑）。"""
        return cls(Path(path))

    # ------------------------------------------------------------------
    # 写入方法（每种 EventType 一个语义化方法）
    # ------------------------------------------------------------------

    def log_task_start(self, task: Task) -> None:
        """任务开始。"""
        self._append(Event(
            event_type=EventType.TASK_START,
            task_id=task.task_id,
            payload={"task": task.to_dict()},
        ))

    def log_action(self, step: int, action: Action, raw_content: str = "") -> None:
        """Agent 的每一步决策。raw_content 是模型返回的完整原始文本。"""
        self._append(Event(
            event_type=EventType.ACTION,
            task_id=self._current_task_id,
            payload={
                "step":        step,
                "action":      action.to_dict(),
                "raw_content": raw_content,  # 模型原始输出，含完整推理链
            },
        ))

    def log_observation(self, step: int, observation: Observation) -> None:
        """工具执行结果。"""
        self._append(Event(
            event_type=EventType.OBSERVATION,
            task_id=self._current_task_id,
            payload={
                "step":        step,
                "observation": observation.to_dict(),
            },
        ))

    def log_reflection(self, step: int, reason: str, prompt: str) -> None:
        """
        触发 Reflection 时记录。
        reason：触发原因（"test_failed" / "no_edit_n_steps"）
        prompt：注入 LLM 的 reflection prompt
        """
        self._append(Event(
            event_type=EventType.REFLECTION,
            task_id=self._current_task_id,
            payload={
                "step":   step,
                "reason": reason,
                "prompt": prompt,
            },
        ))

    def log_plan_generated(self, plan: Plan) -> None:
        """PlanExecuteAgent 成功生成执行计划。"""
        self._append(Event(
            event_type=EventType.PLAN_GENERATED,
            task_id=self._current_task_id,
            payload={"plan": plan.to_dict()},
        ))

    def log_subtask_start(
        self, subtask: SubTask, index: int, total: int
    ) -> None:
        """开始执行一个子任务。"""
        self._append(Event(
            event_type=EventType.SUBTASK_START,
            task_id=self._current_task_id,
            payload={
                "subtask": subtask.to_dict(),
                "index": index,
                "total": total,
            },
        ))

    def log_subtask_complete(
        self, subtask: SubTask, result: RunResult
    ) -> None:
        """子任务成功完成。"""
        self._append(Event(
            event_type=EventType.SUBTASK_COMPLETE,
            task_id=self._current_task_id,
            payload={
                "subtask_id": subtask.id,
                "result": result.to_dict(),
            },
        ))

    def log_subtask_failed(
        self, subtask: SubTask, result: RunResult
    ) -> None:
        """子任务执行失败。"""
        self._append(Event(
            event_type=EventType.SUBTASK_FAILED,
            task_id=self._current_task_id,
            payload={
                "subtask_id": subtask.id,
                "result": result.to_dict(),
            },
        ))

    def log_task_complete(self, steps: int, summary: str) -> None:
        """任务成功完成。"""
        self._append(Event(
            event_type=EventType.TASK_COMPLETE,
            task_id=self._current_task_id,
            payload={
                "steps":   steps,
                "summary": summary,
            },
        ))

    def log_task_failed(self, steps: int, reason: str) -> None:
        """任务失败或被熔断。"""
        self._append(Event(
            event_type=EventType.TASK_FAILED,
            task_id=self._current_task_id,
            payload={
                "steps":  steps,
                "reason": reason,
            },
        ))

    # ------------------------------------------------------------------
    # 读取方法
    # ------------------------------------------------------------------

    def replay(self) -> list[Event]:
        """
        从头读取所有 event，还原完整事件序列。
        用于调试、断点续跑分析。文件关闭后仍可调用。
        """
        if not self._file.closed:
            self._file.flush()
        events: list[Event] = []
        with open(self._path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                events.append(Event(
                    event_id=raw["event_id"],
                    event_type=EventType(raw["event_type"]),
                    task_id=raw["task_id"],
                    timestamp=raw["timestamp"],
                    payload=raw["payload"],
                ))
        return events

    def iter_events(self) -> Iterator[Event]:
        """惰性迭代所有 event，适合大文件。文件关闭后仍可调用。"""
        if not self._file.closed:
            self._file.flush()
        with open(self._path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                yield Event(
                    event_id=raw["event_id"],
                    event_type=EventType(raw["event_type"]),
                    task_id=raw["task_id"],
                    timestamp=raw["timestamp"],
                    payload=raw["payload"],
                )

    def get_actions(self) -> list[Action]:
        """
        从 event log 提取所有 Action，用于循环检测。
        （连续相同 action 时触发熔断）
        """
        from agent.task import ActionType, ToolCall

        actions: list[Action] = []
        for event in self.iter_events():
            if event.event_type != EventType.ACTION:
                continue
            raw_action = event.payload["action"]
            raw_tc = raw_action.get("tool_call")
            tool_call = None
            if raw_tc:
                tool_call = ToolCall(
                    name=raw_tc["name"],
                    params=raw_tc["params"],
                )
            actions.append(Action(
                action_type=ActionType(raw_action["action_type"]),
                thought=raw_action["thought"],
                tool_call=tool_call,
                message=raw_action.get("message"),
            ))
        return actions

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def _current_task_id(self) -> str:
        """从文件名中提取 task_id（8位前缀）。"""
        return self._path.stem.split("_")[0]

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _append(self, event: Event) -> None:
        """
        写入一条 event。
        每次写入后立即 flush，确保崩溃不丢数据。
        """
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        self._file.write(line + "\n")
        self._file.flush()

    def close(self) -> None:
        """显式关闭文件。通常在 Agent.run() 结束时调用。"""
        if not self._file.closed:
            self._file.flush()
            self._file.close()

    def __enter__(self) -> "EventLog":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"EventLog(path={self._path})"


# ---------------------------------------------------------------------------
# 辅助：从已完成的 log 生成摘要统计
# ---------------------------------------------------------------------------

def summarize_run(log: EventLog) -> dict:
    """
    读取一次完整运行的 event log，返回统计摘要。
    用于 Day 7 的分析脚本，不在 agent 主流程里使用。
    """
    events = log.replay()

    stats = {
        "total_events":    len(events),
        "actions":         0,
        "reflections":     0,
        "tool_calls":      {},   # tool_name -> count
        "observations_ok": 0,
        "observations_err": 0,
        "final_status":    None,
    }

    for event in events:
        if event.event_type == EventType.ACTION:
            stats["actions"] += 1
            tc = event.payload["action"].get("tool_call")
            if tc:
                name = tc["name"]
                stats["tool_calls"][name] = stats["tool_calls"].get(name, 0) + 1

        elif event.event_type == EventType.OBSERVATION:
            obs = event.payload["observation"]
            if obs["status"] == "success":
                stats["observations_ok"] += 1
            else:
                stats["observations_err"] += 1

        elif event.event_type == EventType.REFLECTION:
            stats["reflections"] += 1

        elif event.event_type in (EventType.TASK_COMPLETE, EventType.TASK_FAILED):
            stats["final_status"] = event.event_type.value

    return stats