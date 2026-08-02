"""
P12: Runtime execution — frozen snapshot of one turn's state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.eventing.identifiers import SessionId, RunId


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    """Immutable snapshot of conversation state for one turn."""
    messages: tuple[dict, ...] = ()
    system_prompt: str = ""
    project_instructions: str = ""


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """Immutable snapshot of available tools and skills."""
    tool_schemas: tuple[dict, ...] = ()
    active_skills: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeExecution:
    """Immutable execution context for one turn."""
    session_id: SessionId
    run_id: RunId
    turn_index: int = 0
    conversation: ConversationSnapshot = field(default_factory=ConversationSnapshot)
    capabilities: CapabilitySnapshot = field(default_factory=CapabilitySnapshot)
    max_steps: int = 25
    budget_tokens: int = 200_000
