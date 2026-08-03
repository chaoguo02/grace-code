from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agent.event_log import EventLog
from agent.task import EventType


@dataclass
class LogFilterRecord:
    log_path: str
    task_id: str | None = None
    entrypoint: str | None = None
    mode: str | None = None
    session_id: str | None = None
    round: int | None = None
    phase: str | None = None
    parent_task_id: str | None = None
    subtask_id: str | None = None
    subtask_type: str | None = None
    role: str | None = None
    agent_id: str | None = None
    coordinator_task_id: str | None = None
    isolation: str | None = None
    depends_on: list[str] = field(default_factory=list)
    spawn_index: int | None = None
    provider: str | None = None
    model: str | None = None
    final_event: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_log_filter_record(log_path: str | Path) -> LogFilterRecord:
    path = Path(log_path)
    task_payload: dict[str, Any] = {}
    final_event: str | None = None

    with EventLog.open_existing(path) as log:
        for event in log.iter_events():
            if event.event_type == EventType.TASK_START:
                task_payload = event.payload.get("task", {})
            if event.event_type in (EventType.TASK_COMPLETE, EventType.TASK_FAILED):
                final_event = event.event_type.value

    metadata = task_payload.get("metadata", {}) if task_payload else {}
    return LogFilterRecord(
        log_path=str(path),
        task_id=task_payload.get("task_id"),
        entrypoint=metadata.get("entrypoint"),
        mode=metadata.get("mode"),
        session_id=metadata.get("session_id"),
        round=metadata.get("round"),
        phase=metadata.get("phase"),
        parent_task_id=metadata.get("parent_task_id"),
        subtask_id=metadata.get("subtask_id"),
        subtask_type=metadata.get("subtask_type"),
        role=metadata.get("role"),
        agent_id=metadata.get("agent_id"),
        coordinator_task_id=metadata.get("coordinator_task_id"),
        isolation=metadata.get("isolation"),
        depends_on=list(metadata.get("depends_on") or []),
        spawn_index=metadata.get("spawn_index"),
        provider=metadata.get("provider"),
        model=metadata.get("model"),
        final_event=final_event,
    )


def collect_log_filter_records(log_paths: list[str | Path]) -> list[LogFilterRecord]:
    return [extract_log_filter_record(path) for path in log_paths]


def summarize_filter_groups(records: list[LogFilterRecord]) -> dict[str, dict[str, int]]:
    def _count(attr_name: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in records:
            value = getattr(record, attr_name)
            if value:
                counts[str(value)] = counts.get(str(value), 0) + 1
        return counts

    return {
        "session_id": _count("session_id"),
        "parent_task_id": _count("parent_task_id"),
        "subtask_id": _count("subtask_id"),
        "role": _count("role"),
        "agent_id": _count("agent_id"),
    }
