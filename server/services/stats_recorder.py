"""
StatsRecorder — real-time execution stats collector.

Hooks into ``EventBus.publish()`` to record step-level and session-level
metrics into the stats tables as events flow through.

This is a passive observer — it never modifies events or blocks the pipeline.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from server.services.stats_service import StatsService

logger = logging.getLogger(__name__)

# Tools that produce file diffs
_DIFF_TOOLS = frozenset({"Edit", "Write", "file_edit", "file_write"})


class StatsRecorder:
    """Records execution metrics by observing EventBus events.

    Usage:
        recorder = StatsRecorder(stats_service)
        event_bus.recorder = recorder  # called from publish()
    """

    def __init__(self, stats_service: StatsService) -> None:
        self._stats = stats_service
        # Track per-session start time for duration computation
        self._session_start: dict[str, float] = {}
        # Track per-session step tokens (accumulated)
        self._step_tokens: dict[str, int] = {}

    # ── Called from EventBus.publish() ────────────────────────────────────

    def record(self, event: Any, ws_messages: list[dict[str, Any]]) -> None:
        """Process one agent event and its translated WS messages.

        Args:
            event: The original ``agent.task.Event`` object.
            ws_messages: The translated WS message dicts from _translate_event().
        """
        session_id = getattr(event, "session_id", None)
        if not session_id:
            return

        ev_type = getattr(event, "event_type", None)
        if ev_type is None:
            return
        ev_type_str = ev_type.value if hasattr(ev_type, "value") else str(ev_type)
        payload = getattr(event, "payload", {}) or {}

        # Map internal event types to their WS counterparts
        for msg in ws_messages:
            msg_type = msg.get("type", "")

            # ── task_start → record start time ─────────────────────────
            if msg_type == "status" and msg.get("status") == "running":
                self._session_start[session_id] = time.time()
                self._step_tokens[session_id] = 0

            # ── tool_call → record step log ────────────────────────────
            if msg_type == "tool_call":
                step = msg.get("step", 0)
                tool_name = msg.get("name", "")
                # Estimate tokens from params size (rough: ~4 chars/token)
                params_str = str(msg.get("params", {}))
                tok_est = len(params_str) // 4
                self._stats.record_step(
                    session_id,
                    step_number=step,
                    tool_name=tool_name,
                    tool_params=msg.get("params", {}),
                    status="executing",
                    duration_ms=0,
                    tokens=tok_est,
                    timestamp=msg.get("timestamp", ""),
                )

            # ── observation (Edit/Write with diff) → record diff ───────
            if msg_type == "observation":
                diff = msg.get("diff")
                if diff and msg.get("tool_name") in _DIFF_TOOLS:
                    step = msg.get("step", 0)
                    from server.services.event_bus import EventBus
                    m = EventBus._FILE_PATH_RE.search(msg.get("output", ""))
                    file_path = m.group(1) if m else "unknown"
                    self._stats.record_diff(
                        session_id, step_number=step,
                        file_path=file_path, diff_content=diff,
                    )

            # ── task_complete / task_failed → finalize stats ──────────
            if msg_type == "status" and msg.get("status") in ("completed", "failed"):
                self._finalize_session(
                    session_id, msg, ev_type_str, payload,
                )

    # ── Internal ─────────────────────────────────────────────────────────

    def _finalize_session(
        self, session_id: str, msg: dict[str, Any],
        ev_type_str: str, payload: Any,
    ) -> None:
        """Compute aggregate stats and persist."""
        start = self._session_start.pop(session_id, None)
        duration_ms = int((time.time() - start) * 1000) if start else 0

        # Get agent name from the session record
        agent_name = "unknown"
        try:
            from agent.session.session_store import SessionStore
            # Access via StatsService's storage backend
            store = getattr(self._stats, '_storage', None)
            if store is not None:
                rec = store.get_session(session_id)
                if rec is not None:
                    agent_name = rec.agent_name
        except Exception:
            pass

        result = msg.get("result", {}) or {}
        total_steps = result.get("steps_taken", 0)
        # Use actual token count from the event payload when available
        total_tokens = result.get("total_tokens", 0) or self._step_tokens.get(session_id, 0)
        status = msg.get("status", "completed")

        # Build tool_summary from step_log
        steps = self._stats.get_session_steps(session_id)
        tool_summary: dict[str, int] = {}
        for s in steps:
            tn = s.get("tool_name", "unknown")
            tool_summary[tn] = tool_summary.get(tn, 0) + 1

        self._stats.record_session_complete(
            session_id,
            agent_name=agent_name,
            total_steps=total_steps or len(steps),
            total_tokens=total_tokens,
            total_duration_ms=duration_ms,
            status=status,
            tool_summary=tool_summary,
        )

        self._step_tokens.pop(session_id, None)
        logger.info(
            "Stats recorded — session=%s agent=%s steps=%d tokens=%d duration=%dms",
            session_id[:8], agent_name, total_steps, total_tokens, duration_ms,
        )
