"""
P12: Runtime ports — frozen dependency injection contract.

All ports are Protocol-based.  Runtime only depends on these, not on
concrete implementations (agent, server, SQLite).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from core.eventing.identifiers import SessionId, RunId


class RunEventPort(Protocol):
    """Publish run lifecycle facts."""
    def publish(self, event) -> None: ...


class StatsPort(Protocol):
    """Record performance metrics."""
    def record(self, run_id: RunId, metric_name: str, value: float) -> None: ...


class ContextPort(Protocol):
    """Provide conversation context for a turn."""
    def get_context(self, session_id: SessionId) -> object: ...


class ToolPort(Protocol):
    """Execute a tool call."""
    def execute(self, tool_name: str, params: dict, invocation_id: str) -> object: ...


class LLMPort(Protocol):
    """Call the language model."""
    def invoke(self, messages: list[dict], tools: list[dict]) -> object: ...


@dataclass(frozen=True, slots=True)
class RuntimePorts:
    """Immutable dependency set injected at Runtime construction."""
    events: RunEventPort | None = None
    stats: StatsPort | None = None
    context: ContextPort | None = None
    tools: ToolPort | None = None
    llm: LLMPort | None = None
    web_mode: bool = False
