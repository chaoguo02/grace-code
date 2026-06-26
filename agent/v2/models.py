from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionRecord:
    id: str
    parent_id: str | None
    root_id: str
    agent_name: str
    mode: str
    title: str
    status: str
    repo_path: str
    summary: str = ""
    error: str = ""
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionMessageRecord:
    id: int
    session_id: str
    role: str
    content: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    created_at: str = ""


@dataclass(frozen=True)
class ChildSessionResult:
    session_id: str
    status: str
    summary: str
    artifacts: tuple[str, ...] = ()
    missing_info: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "summary": self.summary,
            "artifacts": list(self.artifacts),
            "missing_info": self.missing_info,
            "error": self.error,
        }


@dataclass(frozen=True)
class AgentSpec:
    name: str
    mode: str
    allowed_tools: frozenset[str]
    allow_task_tool: bool = False
    hidden: bool = False
    description: str = ""
