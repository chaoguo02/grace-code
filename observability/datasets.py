from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agent.event_log import EventLog, summarize_run
from agent.task import RunResult, RunStatus, Task


DEFAULT_FAILURE_DATASET_PATH = Path(".forge-agent") / "datasets" / "forge-agent-failures.jsonl"


@dataclass(frozen=True)
class FailureDatasetItem:
    id: str
    dataset_name: str
    input: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    expected_output: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def should_capture_failure_dataset(task: Task, result: RunResult) -> bool:
    if result.status == RunStatus.SUCCESS:
        return False
    if task.metadata.get("parent_task_id"):
        return False
    return True


def build_failure_dataset_item(
    task: Task,
    result: RunResult,
    *,
    log_path: str | Path | None = None,
    stats: dict[str, Any] | None = None,
    dataset_name: str = "forge-agent/failures",
) -> FailureDatasetItem:
    stats = stats or {}
    final_reason = result.error or result.summary or result.status.value
    item_id = f"{task.task_id}:{result.status.value}"

    return FailureDatasetItem(
        id=item_id,
        dataset_name=dataset_name,
        input={
            "task": task.description,
            "repo_path": task.repo_path,
            "intent": task.intent,
            "issue_url": task.issue_url,
        },
        metadata={
            "task_id": task.task_id,
            "final_status": result.status.value,
            "final_reason": final_reason,
            "summary": result.summary,
            "error": result.error,
            "steps_taken": result.steps_taken,
            "total_tokens": result.total_tokens,
            "entrypoint": task.metadata.get("entrypoint"),
            "mode": task.metadata.get("mode"),
            "session_id": task.metadata.get("session_id"),
            "round": task.metadata.get("round"),
            "provider": task.metadata.get("provider"),
            "model": task.metadata.get("model"),
            "tool_error_count": stats.get("observations_err", 0),
            "reflection_count": stats.get("reflections", 0),
            "action_count": stats.get("actions", 0),
            "tool_calls": stats.get("tool_calls", {}),
            "log_path": str(log_path) if log_path else None,
        },
    )


def append_failure_dataset_item(
    task: Task,
    result: RunResult,
    *,
    log_path: str | Path | None = None,
    stats: dict[str, Any] | None = None,
    dataset_path: str | Path | None = None,
    dataset_name: str = "forge-agent/failures",
) -> Path | None:
    if not should_capture_failure_dataset(task, result):
        return None

    resolved_dataset_path = _resolve_dataset_path(task.repo_path, dataset_path)
    stats = stats or _load_log_stats(log_path)
    item = build_failure_dataset_item(
        task,
        result,
        log_path=log_path,
        stats=stats,
        dataset_name=dataset_name,
    )

    resolved_dataset_path.parent.mkdir(parents=True, exist_ok=True)
    with open(resolved_dataset_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
    return resolved_dataset_path


def _resolve_dataset_path(repo_path: str, dataset_path: str | Path | None) -> Path:
    if dataset_path is None:
        return Path(repo_path) / DEFAULT_FAILURE_DATASET_PATH
    return Path(dataset_path)


def _load_log_stats(log_path: str | Path | None) -> dict[str, Any]:
    if log_path is None:
        return {}

    path = Path(log_path)
    if not path.exists():
        return {}

    with EventLog.open_existing(path) as elog:
        return summarize_run(elog)
