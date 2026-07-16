"""
hooks/events.py

Hook event types and context dataclass.

Event lifecycle aligned with Claude Code's hook system:
- PreToolUse: before tool execution (blockable)
- PostToolUse: after successful tool execution
- PostToolUseFailure: after failed tool execution
- SessionStart: session begins
- Stop: agent finishes responding
- UserPromptSubmit: user input received (blockable)
- SubagentStart: child session starts
- SubagentStop: child session completes
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class HookEvent(str, Enum):
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    POST_TOOL_USE_FAILURE = "PostToolUseFailure"
    SESSION_START = "SessionStart"
    STOP = "Stop"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"


class SessionStartSource(str, Enum):
    """Objective reason a session-start lifecycle event was emitted."""

    STARTUP = "startup"
    RESUME = "resume"
    CLEAR = "clear"


BLOCKABLE_EVENTS: frozenset[HookEvent] = frozenset({
    HookEvent.PRE_TOOL_USE,
    HookEvent.USER_PROMPT_SUBMIT,
    HookEvent.STOP,
    HookEvent.SUBAGENT_STOP,
})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class HookContext:
    """Context passed to hooks via stdin JSON and to internal callbacks."""

    event: HookEvent
    session_id: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_output: dict[str, Any] | None = None
    user_input: str = ""
    messages: list[Any] | None = None
    agent_id: str = ""
    agent_type: str = ""
    last_assistant_message: str = ""
    stop_hook_active: bool = False
    session_start_source: SessionStartSource | None = None
    timestamp: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        self.event = HookEvent(self.event)
        if self.session_start_source is not None:
            self.session_start_source = SessionStartSource(
                self.session_start_source
            )
        if (
            self.event is HookEvent.SESSION_START
            and self.session_start_source is None
        ):
            self.session_start_source = SessionStartSource.STARTUP

    @property
    def matcher_subject(self) -> str:
        """Return the declarative matcher dimension for this event."""
        if self.event in {
            HookEvent.SUBAGENT_START,
            HookEvent.SUBAGENT_STOP,
        }:
            return self.agent_type
        if self.event is HookEvent.SESSION_START:
            assert self.session_start_source is not None
            return self.session_start_source.value
        return self.tool_name

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "event": self.event.value,
            "hook_event_name": self.event.value,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
        }
        if self.tool_name:
            d["tool_name"] = self.tool_name
        if self.tool_input:
            d["tool_input"] = self.tool_input
        if self.tool_output is not None:
            d["tool_output"] = self.tool_output
        if self.user_input:
            d["user_input"] = self.user_input
        if self.messages is not None:
            d["messages"] = self.messages
        if self.agent_id:
            d["agent_id"] = self.agent_id
        if self.agent_type:
            d["agent_type"] = self.agent_type
        if self.last_assistant_message:
            d["last_assistant_message"] = self.last_assistant_message
        if self.event in {HookEvent.STOP, HookEvent.SUBAGENT_STOP}:
            d["stop_hook_active"] = self.stop_hook_active
        return d
