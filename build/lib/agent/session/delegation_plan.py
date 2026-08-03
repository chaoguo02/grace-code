"""DelegationPlanV1 / DelegationTaskV1 — shared DAG schema.

Used by:
  - AgentBatch schema (JSON → typed model)
  - Runtime validation (topology checks)
  - SessionStore persistence
  - MultiAgentService projection
  - Frontend types (TypeScript mirror)

One schema everywhere. No gradual divergence between JSON, Python,
database, and frontend representations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class DelegationTaskV1:
    """One worker task in a delegation DAG."""

    schema_version: int = 1
    id: str = ""
    agent: str = ""
    goal: str = ""
    prompt: str = ""
    purpose: str = "general"
    depends_on: tuple[str, ...] = ()
    scope: tuple[str, ...] = ()
    expected_files: tuple[str, ...] = ()
    write_files: tuple[str, ...] = ()
    required: bool = True
    model: str | None = None
    isolation: str | None = None
    acceptance: tuple[str, ...] = ()
    """Verifiable acceptance criteria. Empty = no explicit acceptance."""

    def __post_init__(self) -> None:
        if self.schema_version < 1:
            raise ValueError("schema_version must be >= 1")
        if not self.id or not self.id.strip():
            raise ValueError("task id is required")
        if not self.goal or not self.goal.strip():
            raise ValueError("task goal is required")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "agent": self.agent,
            "goal": self.goal,
            "prompt": self.prompt,
            "purpose": self.purpose,
            "depends_on": list(self.depends_on),
            "scope": list(self.scope),
            "expected_files": list(self.expected_files),
            "write_files": list(self.write_files),
            "required": self.required,
            "model": self.model,
            "isolation": self.isolation,
            "acceptance": list(self.acceptance),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "DelegationTaskV1":
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            id=str(data.get("id", "")),
            agent=str(data.get("agent", "")),
            goal=str(data.get("goal", "")),
            prompt=str(data.get("prompt", "")),
            purpose=str(data.get("purpose", "general")),
            depends_on=tuple(str(v) for v in data.get("depends_on", [])),
            scope=tuple(str(v) for v in data.get("scope", [])),
            expected_files=tuple(str(v) for v in data.get("expected_files", [])),
            write_files=tuple(str(v) for v in data.get("write_files", [])),
            required=bool(data.get("required", True)),
            model=str(data["model"]) if data.get("model") else None,
            isolation=str(data["isolation"]) if data.get("isolation") else None,
            acceptance=tuple(str(v) for v in data.get("acceptance", [])),
        )


@dataclass(frozen=True)
class DelegationPlanV1:
    """One delegation run — a bounded DAG of tasks."""

    schema_version: int = 1
    description: str = ""
    topology: Literal["fan_out_fan_in", "chain"] = "fan_out_fan_in"
    reason_code: str = ""
    explanation: str = ""
    tasks: tuple[DelegationTaskV1, ...] = ()
    acceptance: tuple[str, ...] = ()
    """Run-level acceptance criteria."""

    def __post_init__(self) -> None:
        if self.schema_version < 1:
            raise ValueError("schema_version must be >= 1")
        if len(self.tasks) < 2:
            raise ValueError("DelegationPlanV1 requires at least 2 tasks")
        task_ids = {t.id for t in self.tasks}
        if len(task_ids) != len(self.tasks):
            raise ValueError("task ids must be unique")
        for task in self.tasks:
            unknown = set(task.depends_on) - task_ids
            if unknown:
                raise ValueError(
                    f"Task {task.id!r} depends on unknown tasks: {sorted(unknown)}"
                )
            if task.id in task.depends_on:
                raise ValueError(f"Task {task.id!r} cannot depend on itself")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "description": self.description,
            "topology": self.topology,
            "reason_code": self.reason_code,
            "explanation": self.explanation,
            "tasks": [t.to_dict() for t in self.tasks],
            "acceptance": list(self.acceptance),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "DelegationPlanV1":
        tasks_raw = data.get("tasks", [])
        tasks = tuple(
            DelegationTaskV1.from_dict(t)
            for t in tasks_raw
            if isinstance(t, dict)
        )
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            description=str(data.get("description", "")),
            topology=str(data.get("topology", "fan_out_fan_in")),
            reason_code=str(data.get("reason_code", "")),
            explanation=str(data.get("explanation", "")),
            tasks=tasks,
            acceptance=tuple(str(v) for v in data.get("acceptance", [])),
        )
