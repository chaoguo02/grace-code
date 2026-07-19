"""
StatsRecorder — first-party execution stats collector.

Called directly from the ReActAgent loop (agent/core.py), NOT as an
EventBus side effect.  This gives the recorder access to structured
data (tool names, success/failure, duration) and session metadata
(agent_name, session_id) without depending on WS message format.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from server.services.stats_service import StatsService

logger = logging.getLogger(__name__)


class StatsRecorder:
    """First-party stats collector called from the agent loop.

    Records tool executions and session lifecycle events.
    The agent loop calls these methods directly — no EventBus dependency.
    """

    def __init__(self, stats_service: StatsService) -> None:
        self._stats = stats_service
        self._session_start: dict[str, float] = {}

    def record_session_start(self, session_id: str, agent_name: str) -> None:
        """Called when the agent begins execution."""
        self._session_start[session_id] = time.time()
        logger.debug("Stats: session %s started (agent=%s)", session_id[:8], agent_name)

    def record_tool_call(
        self, *, session_id: str, agent_name: str,
        step: int, tool_name: str,
        success: bool, duration_ms: float,
    ) -> None:
        """Called after each tool execution in the agent loop."""
        self._stats.record_step(
            session_id,
            step_number=step,
            tool_name=tool_name,
            tool_params={},
            status="success" if success else "error",
            duration_ms=int(duration_ms),
            tokens=0,
            timestamp="",
        )

    def record_session_end(
        self, session_id: str, *,
        total_steps: int, total_tokens: int,
        status: str = "completed",
    ) -> None:
        """Called when the agent finishes (success or failure)."""
        start = self._session_start.pop(session_id, None)
        duration_ms = int((time.time() - start) * 1000) if start else 0

        steps = self._stats.get_session_steps(session_id)
        tool_summary: dict[str, int] = {}
        for s in steps:
            tn = s.get("tool_name", "")
            if tn:
                tool_summary[tn] = tool_summary.get(tn, 0) + 1

        # Agent name unknown here — the caller (agent loop) doesn't pass it.
        # Stored as empty; the session detail API can fill it from session record.
        self._stats.record_session_complete(
            session_id,
            agent_name="",  # filled by session detail API
            total_steps=total_steps or len(steps),
            total_tokens=total_tokens,
            total_duration_ms=duration_ms,
            status=status,
            tool_summary=tool_summary,
        )

        logger.info(
            "Stats finalized — session=%s steps=%d tokens=%d duration=%dms",
            session_id[:8], total_steps, total_tokens, duration_ms,
        )

    # Backward compat — called from EventBus.publish(). No-op: first-party
    # recording is handled by the agent loop now. Kept for interface compat.
    def set_session_agent(self, session_id: str, agent_name: str) -> None:
        pass
