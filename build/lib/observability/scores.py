from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.task import RunResult, RunStatus, Task


@dataclass(frozen=True)
class ScoreRecord:
    name: str
    value: float | bool | str
    comment: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

def build_run_scores(
    task: Task,
    result: RunResult,
    *,
    stats: dict[str, Any] | None = None,
) -> list[ScoreRecord]:
    stats = stats or {}
    base_metadata = {
        "status": result.status.value,
        "intent": task.intent,
        "entrypoint": task.metadata.get("entrypoint"),
        "mode": task.metadata.get("mode"),
    }

    scores = [
        ScoreRecord(
            name="grace.task_success",
            value=1.0 if result.status == RunStatus.SUCCESS else 0.0,
            metadata=base_metadata,
        ),
        ScoreRecord(
            name="grace.task_error_free",
            value=1.0 if not result.error else 0.0,
            metadata=base_metadata,
        ),
        ScoreRecord(
            name="grace.task_max_steps_exhausted",
            value=1.0 if result.status == RunStatus.MAX_STEPS else 0.0,
            metadata=base_metadata,
        ),
        ScoreRecord(
            name="grace.task_gave_up",
            value=1.0 if result.status == RunStatus.GAVE_UP else 0.0,
            metadata=base_metadata,
        ),
        ScoreRecord(
            name="grace.task_tool_error_count",
            value=float(stats.get("observations_err", 0)),
            metadata=base_metadata,
        ),
        ScoreRecord(
            name="grace.task_reflection_count",
            value=float(stats.get("reflections", 0)),
            metadata=base_metadata,
        ),
        ScoreRecord(
            name="grace.task_tool_decision_count",
            value=float(stats.get("tool_decisions", 0)),
            metadata=base_metadata,
        ),
        ScoreRecord(
            name="grace.task_recovery_action_count",
            value=float(stats.get("recovery_actions", 0)),
            metadata=base_metadata,
        ),
    ]

    if task.intent == "analysis":
        scores.extend(
            [
                ScoreRecord(
                    name="grace.analysis_claim_count",
                    value=float(stats.get("claims_created", 0)),
                    metadata=base_metadata,
                ),
                ScoreRecord(
                    name="grace.analysis_deferred_read_count",
                    value=float(stats.get("analysis_deferred_reads", 0)),
                    metadata=base_metadata,
                ),
            ]
        )

    if task.intent == "edit":
        scores.append(
            ScoreRecord(
                name="grace.task_patch_generated",
                value=1.0 if bool(result.patch) else 0.0,
                metadata=base_metadata,
            )
        )

    return scores
