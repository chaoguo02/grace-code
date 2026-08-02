"""
P3: Tool execution facts — independent payload classes.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.eventing.identifiers import RunId, TaskId


@dataclass(frozen=True, slots=True)
class ToolExecutedV1:
    run_id: RunId
    task_id: TaskId | None = None  # None for primary agent, set for child tasks
    tool_name: str = ""
    invocation_id: str = ""
    success: bool = True
    duration_ms: float = 0.0
    is_error: bool = False

    def __post_init__(self) -> None:
        if not self.tool_name:
            raise ValueError("ToolExecutedV1: tool_name must not be empty")
        if self.duration_ms < 0:
            raise ValueError(f"duration_ms must be >= 0, got {self.duration_ms}")
