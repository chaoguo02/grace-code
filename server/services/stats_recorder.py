"""
StatsRecorder — execution stats collector.

Hooks directly into ``agent.task.Event`` objects — NOT translated
WS messages.  This ensures the recorder has access to the complete
structured payload (tool names, statuses, results) without depending
on the frontend-facing message format.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from server.services.stats_service import StatsService

logger = logging.getLogger(__name__)

# Tools whose execution may modify workspace files.
_DIFF_TOOLS = frozenset({"Edit", "Write", "file_edit", "file_write", "Bash"})


class StatsRecorder:
    """Records execution metrics from raw agent Events.

    Hooks into ``EventBus.publish()`` as a passive observer.
    Extracts structured data from ``agent.task.Event`` payloads,
    not from the translated WS messages.
    """

    def __init__(self, stats_service: StatsService) -> None:
        self._stats = stats_service
        self._session_start: dict[str, float] = {}
        self._session_agent: dict[str, str] = {}

    def set_session_agent(self, session_id: str, agent_name: str) -> None:
        """Register the agent name for a session (called at session start)."""
        self._session_agent[session_id] = agent_name

    def record(self, event: Any, ws_messages: list[dict[str, Any]]) -> None:
        """Process one agent Event.

        Extracts structured data from the raw Event payload.
        *ws_messages* is ignored — kept for backward compat with EventBus.
        """
        session_id = getattr(event, "session_id", None)
        if not session_id:
            return

        ev_type = getattr(event, "event_type", None)
        if ev_type is None:
            return
        ev_type_str = ev_type.value if hasattr(ev_type, "value") else str(ev_type)
        payload = getattr(event, "payload", {}) or {}
        ts = getattr(event, "timestamp", "")

        # ── task_start — record session metadata ──────────────────────
        if ev_type_str == "task_start":
            self._session_start[session_id] = time.time()

        # ── action — record tool invocations ──────────────────────────
        elif ev_type_str == "action":
            action = payload.get("action", {}) or {}
            step = payload.get("step", 0)
            for tc in (action.get("tool_calls") or []):
                tool_name = tc.get("name", "")
                tool_params = tc.get("params", {})
                self._stats.record_step(
                    session_id,
                    step_number=step,
                    tool_name=tool_name,
                    tool_params=tool_params,
                    status="executing",
                    duration_ms=0,
                    tokens=0,
                    timestamp=ts,
                )

        # ── observation — record result + diff ────────────────────────
        elif ev_type_str == "observation":
            obs = payload.get("observation", {}) or {}
            step = payload.get("step", 0)
            tool_name = obs.get("tool_name", "")
            status = obs.get("status", "")
            # Record step as success/error based on observation
            if status:
                self._stats.record_step(
                    session_id,
                    step_number=step,
                    tool_name=tool_name,
                    tool_params={},
                    status=status,
                    duration_ms=0,
                    tokens=0,
                    timestamp=ts,
                )

        # ── task_complete / task_failed — finalize ─────────────────────
        elif ev_type_str in ("task_complete", "task_failed"):
            start = self._session_start.pop(session_id, None)
            duration_ms = int((time.time() - start) * 1000) if start else 0
            agent_name = self._session_agent.pop(session_id, "unknown")

            is_complete = ev_type_str == "task_complete"
            result = payload if is_complete else {}
            total_steps = payload.get("steps", 0) if is_complete else 0
            total_tokens = 0  # tokens tracked by agent loop, not in Event
            status = "completed" if is_complete else "failed"

            steps = self._stats.get_session_steps(session_id)
            tool_summary: dict[str, int] = {}
            for s in steps:
                tn = s.get("tool_name", "")
                if tn:
                    tool_summary[tn] = tool_summary.get(tn, 0) + 1

            self._stats.record_session_complete(
                session_id,
                agent_name=agent_name or "unknown",
                total_steps=total_steps or len(steps),
                total_tokens=total_tokens,
                total_duration_ms=duration_ms,
                status=status,
                tool_summary=tool_summary,
            )

            logger.info(
                "Stats finalized — session=%s agent=%s steps=%d duration=%dms",
                session_id[:8], agent_name, total_steps or len(steps), duration_ms,
            )
