"""
G30: Runtime Replay — offline, no side effects, recorded tool results.

Replays recorded inputs through Runtime to produce deterministic outcomes.
ToolPort returns recorded results, never executes real tools.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from core.eventing.identifiers import RunId
from runtime_core.execution import RuntimeExecution, ConversationSnapshot
from runtime_core.model_actions import (
    ModelAction, AssistantText, ToolCall, ToolCallBatch,
    ModelStop, ModelRefusal, ModelFailure,
)
from runtime_core.outcome import RuntimeOutcome


@dataclass
class RecordedTurn:
    """One recorded turn: model action + tool results."""
    turn_index: int
    model_action: dict  # serialized ModelAction
    tool_results: list[dict] = field(default_factory=list)
    hook_decisions: list[dict] = field(default_factory=list)


@dataclass
class ReplayInput:
    """Desensitized replay input."""
    session_id: str
    run_id: str
    turns: list[RecordedTurn]
    max_steps: int = 25


class ReplayToolPort:
    """ToolPort that returns recorded results — no real side effects."""

    def __init__(self, recorded_results: dict[str, dict]) -> None:
        self._recorded = recorded_results  # tool_call_id → result

    def execute(self, tool_name: str, params, invocation_id: str = ""):
        from runtime_core.ports import ToolSuccess
        if invocation_id in self._recorded:
            rec = self._recorded[invocation_id]
            return ToolSuccess(
                tool_name=tool_name,
                output=rec.get("output", ""),
                duration_ms=rec.get("duration_ms", 0),
            )
        return ToolSuccess(tool_name=tool_name, output="")


class ReplayLLMPort:
    """LLMPort that returns recorded model actions — no real LLM calls."""

    def __init__(self, turns: list[RecordedTurn]) -> None:
        self._turns = turns
        self._index = 0

    def invoke(self, messages, tools=None) -> ModelAction:
        if self._index >= len(self._turns):
            return AssistantText(text="end")
        turn = self._turns[self._index]
        self._index += 1
        action = turn.model_action
        action_type = action.get("type", "assistant_text")
        if action_type == "assistant_text":
            return AssistantText(text=action.get("text", ""))
        if action_type == "tool_call":
            return ToolCall(
                id=action.get("id", ""),
                name=action.get("name", ""),
                params=action.get("params", {}),
            )
        if action_type == "model_stop":
            return ModelStop(stop_reason=action.get("stop_reason", ""))
        return AssistantText(text="")

    def stream(self, messages, tools=None):
        async def _s():
            return self.invoke(messages, tools)
        return _s()


class NullProjectionSink:
    """In-memory projection sink — no DB writes."""
    def __init__(self) -> None:
        self.events: list = []

    def on_event(self, envelope):
        self.events.append(str(envelope.event_type))
        from eventing.subscriber import DeliveryReceipt
        return DeliveryReceipt.ok(str(envelope.event_id), "null_sink")
