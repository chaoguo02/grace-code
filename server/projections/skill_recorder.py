"""
P5: Skill activation recording projection.

Subscribes to ToolExecuted events.  When a Skill tool is invoked,
records the activation for evidence tracking.  This is a pure
projection — it does not mutate Runtime state or drive business logic.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class SkillActivationRecorder:
    """Records skill invocations from ToolExecuted events.

    Usage:
        recorder = SkillActivationRecorder()
        event_bus.subscribe(ToolExecuted, recorder.on_tool_executed)
    """

    def __init__(self) -> None:
        self._activations: list[dict] = []

    def on_tool_executed(self, event: object) -> None:
        """Handle ToolExecuted — record if it's a Skill tool invocation."""
        tool_name = getattr(event, "tool_name", "")
        if tool_name != "Skill":
            return

        invocation_id = getattr(event, "invocation_id", "")
        session_id = getattr(event, "session_id", "")

        self._activations.append({
            "session_id": session_id,
            "invocation_id": invocation_id,
            "success": getattr(event, "success", True),
        })
        logger.debug(
            "Skill activation recorded: session=%s inv=%s success=%s",
            session_id, invocation_id, getattr(event, "success", True),
        )

    def flush(self) -> list[dict]:
        """Return and clear recorded activations."""
        result = list(self._activations)
        self._activations.clear()
        return result
