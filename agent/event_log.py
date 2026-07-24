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
from typing import Iterator, Any

from agent.task import Event, EventType, ObservationStatus, Task, Action, Observation, RunResult
from observability.models import (
    ReplayContractSnapshot,
    ReplayRunRecord,
    ReplayStepRecord,
    dataclass_to_dict,
)


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
    def create(cls, task: Task, log_dir: str = "") -> "EventLog":
        """
        为一次新运行创建 EventLog。
        目录不存在时自动创建。
        """
        configured = Path(log_dir).expanduser() if log_dir else None
        if configured is not None and configured.is_absolute():
            # An absolute path is an explicit caller-owned export location.
            # Only defaults and relative paths are framework-private state.
            log_path = configured.resolve()
        else:
            from core.state_paths import ProjectStatePaths
            state_paths = ProjectStatePaths.for_project(task.repo_path)
            if not log_dir or log_dir in {"logs", "./logs", ".\\logs"}:
                log_path = state_paths.logs
            else:
                log_path = state_paths.root / configured
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

    def log_observation(self, step: int, observation: Observation, *, tool_call_id: str | None = None) -> None:
        """工具执行结果。"""
        payload: dict[str, Any] = {
            "step":        step,
            "observation": observation.to_dict(),
        }
        if tool_call_id is not None:
            payload["tool_call_id"] = tool_call_id
        self._append(Event(
            event_type=EventType.OBSERVATION,
            task_id=self._current_task_id,
            payload=payload,
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

    def log_phase_start(
        self,
        *,
        step: int,
        phase: str,
        reason: str,
        tokens_so_far: int = 0,
    ) -> None:
        self._append(Event(
            event_type=EventType.PHASE_START,
            task_id=self._current_task_id,
            payload={
                "step": step,
                "phase": phase,
                "reason": reason,
                "tokens_so_far": tokens_so_far,
            },
        ))

    def log_phase_end(
        self,
        *,
        step: int,
        phase: str,
        reason: str,
        tokens_total: int = 0,
        llm_calls: int = 0,
    ) -> None:
        self._append(Event(
            event_type=EventType.PHASE_END,
            task_id=self._current_task_id,
            payload={
                "step": step,
                "phase": phase,
                "reason": reason,
                "tokens_total": tokens_total,
                "llm_calls": llm_calls,
            },
        ))

    def log_tool_decision(
        self,
        *,
        step: int,
        tool_name: str,
        allowed: bool,
        reason: str,
        path: str = "",
        phase: str = "",
    ) -> None:
        self._append(Event(
            event_type=EventType.TOOL_DECISION,
            task_id=self._current_task_id,
            payload={
                "step": step,
                "tool_name": tool_name,
                "allowed": allowed,
                "reason": reason,
                "path": path,
                "phase": phase,
            },
        ))

    def log_recovery_action(
        self,
        *,
        step: int,
        kind: str,
        reason: str,
        prompt: str = "",
        summary: str = "",
    ) -> None:
        self._append(Event(
            event_type=EventType.RECOVERY_ACTION,
            task_id=self._current_task_id,
            payload={
                "step": step,
                "kind": kind,
                "reason": reason,
                "prompt": prompt,
                "summary": summary,
            },
        ))

    def log_claim_created(
        self,
        *,
        step: int,
        phase: str,
        claim,
    ) -> None:
        self._append(Event(
            event_type=EventType.CLAIM_CREATED,
            task_id=self._current_task_id,
            payload={
                "step": step,
                "phase": phase,
                "claim": claim.to_dict(),
            },
        ))

    def log_analysis_phase(
        self,
        step: int,
        previous_phase: str,
        current_phase: str,
        reason: str,
        files_read: int,
        inspect_reads: int,
        verify_reads: int,
    ) -> None:
        """记录 broad analysis phase transition。"""
        self._append(Event(
            event_type=EventType.ANALYSIS_PHASE,
            task_id=self._current_task_id,
            payload={
                "step": step,
                "previous_phase": previous_phase,
                "current_phase": current_phase,
                "reason": reason,
                "files_read": files_read,
                "inspect_reads": inspect_reads,
                "verify_reads": verify_reads,
            },
        ))

    def log_evidence_record(self, step: int, record) -> None:
        """记录完整 EvidenceRecord 明细。"""
        self._append(Event(
            event_type=EventType.EVIDENCE_RECORD,
            task_id=self._current_task_id,
            payload={
                "step": step,
                "record": record.to_dict(),
            },
        ))

    def log_task_complete(self, steps: int, summary: str, contract: dict | None = None, cache_stats: dict | None = None) -> None:
        """任务成功完成。"""
        payload: dict = {
            "steps":   steps,
            "summary": summary,
        }
        if contract:
            payload["contract"] = contract
        if cache_stats:
            payload["cache"] = cache_stats
        self._append(Event(
            event_type=EventType.TASK_COMPLETE,
            task_id=self._current_task_id,
            payload=payload,
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
    # Replay contract methods
    # ------------------------------------------------------------------

    def log_replay_step(self, step_record: ReplayStepRecord) -> None:
        """Emit a single replay step record."""
        self._append(Event(
            event_type=EventType.REPLAY_STEP,
            task_id=self._current_task_id,
            payload=dataclass_to_dict(step_record),
        ))

    def log_replay_run(self, run_record: ReplayRunRecord) -> None:
        """Emit the full replay run record at end of execution."""
        self._append(Event(
            event_type=EventType.REPLAY_RUN,
            task_id=self._current_task_id,
            payload=dataclass_to_dict(run_record),
        ))

    def log_replay_snapshot(self, snapshot: ReplayContractSnapshot) -> None:
        """Emit a complete contract snapshot (version + run)."""
        self._append(Event(
            event_type=EventType.REPLAY_RUN,
            task_id=self._current_task_id,
            payload=dataclass_to_dict(snapshot),
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
            # 兼容旧格式：新日志用 "tool_calls"，旧日志用 "tool_call"
            tool_calls = _parse_tool_calls_from_dict(raw_action)
            actions.append(Action(
                action_type=ActionType(raw_action["action_type"]),
                thought=raw_action["thought"],
                tool_calls=tool_calls,
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
        "phase_starts":    0,
        "phase_ends":      0,
        "tool_decisions":  0,
        "recovery_actions": 0,
        "claims_created":  0,
        "subtasks_skipped": 0,
        "tool_calls":      {},   # tool_name -> count
        "observations_ok": 0,
        "observations_err": 0,
        "analysis_deferred_reads": 0,
        "analysis_phase_token_costs": {},
        "analysis_phase_llm_calls": {},
        "final_status":    None,
    }

    for event in events:
        if event.event_type == EventType.ACTION:
            stats["actions"] += 1
            # 兼容新旧格式，遍历所有 tool calls
            tcs = _parse_tool_calls_from_dict(event.payload["action"])
            for tc in tcs:
                stats["tool_calls"][tc.name] = stats["tool_calls"].get(tc.name, 0) + 1

        elif event.event_type == EventType.OBSERVATION:
            obs = event.payload["observation"]
            if obs["status"] == ObservationStatus.SUCCESS.value:
                stats["observations_ok"] += 1
            else:
                stats["observations_err"] += 1

        elif event.event_type == EventType.REFLECTION:
            stats["reflections"] += 1

        elif event.event_type == EventType.PHASE_START:
            stats["phase_starts"] += 1

        elif event.event_type == EventType.PHASE_END:
            stats["phase_ends"] += 1
            phase = event.payload.get("phase", "")
            if phase:
                stats["analysis_phase_token_costs"][phase] = int(event.payload.get("tokens_total", 0))
                stats["analysis_phase_llm_calls"][phase] = int(event.payload.get("llm_calls", 0))

        elif event.event_type == EventType.TOOL_DECISION:
            stats["tool_decisions"] += 1
            if event.payload.get("allowed") is False:
                stats["analysis_deferred_reads"] += 1

        elif event.event_type == EventType.RECOVERY_ACTION:
            stats["recovery_actions"] += 1

        elif event.event_type == EventType.CLAIM_CREATED:
            stats["claims_created"] += 1

        elif event.event_type == EventType.SUBTASK_SKIPPED:
            stats["subtasks_skipped"] += 1

        elif event.event_type in (EventType.TASK_COMPLETE, EventType.TASK_FAILED):
            stats["final_status"] = event.event_type.value

    return stats


# ---------------------------------------------------------------------------
# 兼容辅助：从序列化 dict 中解析 tool_calls
# ---------------------------------------------------------------------------

def _parse_tool_calls_from_dict(raw_action: dict) -> list:
    """
    从序列化的 action dict 中提取 tool_calls 列表。
    兼容新旧格式：
    - 新日志: "tool_calls": [{"name": "...", "params": {...}}, ...]
    - 旧日志: "tool_call": {"name": "...", "params": {...}} 或 null
    """
    from agent.task import ToolCall

    # 优先读新格式
    raw_list = raw_action.get("tool_calls")
    if isinstance(raw_list, list):
        return [
            ToolCall(name=tc["name"], params=tc["params"])
            for tc in raw_list
        ]

    # 兼容旧格式：单条 tool_call
    raw_single = raw_action.get("tool_call")
    if raw_single:
        return [ToolCall(name=raw_single["name"], params=raw_single["params"])]

    return []
