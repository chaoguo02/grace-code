"""
StatsRecorder — first-party execution stats collector.

Called directly from the ReActAgent loop (agent/core.py), NOT as an
EventBus side effect.  This gives the recorder access to structured
data (tool names, success/failure, duration) and session metadata
(agent_name, session_id) without depending on WS message format.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import logging
import time
from typing import Any

from server.services.stats_service import StatsService

logger = logging.getLogger(__name__)

_SENSITIVE_PARAM_TERMS = {
    "token", "secret", "password", "key", "authorization", "cookie",
    "prompt", "content", "body", "command", "cmd", "script",
}


def _redacted_param_summary(params: dict[str, Any]) -> dict[str, Any]:
    """Return diagnostic shape and fingerprints without retaining payloads."""
    result: dict[str, Any] = {}
    for key, value in params.items():
        text = str(value)
        sensitive = any(term in key.lower() for term in _SENSITIVE_PARAM_TERMS)
        if sensitive or len(text) > 120:
            result[key] = {
                "redacted": True,
                "type": type(value).__name__,
                "length": len(text),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
            }
        elif isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
        else:
            result[key] = {
                "redacted": True,
                "type": type(value).__name__,
                "length": len(text),
            }
    return result


class StatsRecorder:
    """First-party stats collector called from the agent loop.

    Records tool executions and session lifecycle events.
    The agent loop calls these methods directly — no EventBus dependency.
    """

    def __init__(self, stats_service: StatsService) -> None:
        self._stats = stats_service
        self._session_start: dict[str, float] = {}
        self._session_agent: dict[str, str] = {}

    def set_session_agent(self, session_id: str, agent_name: str) -> None:
        """Called when the effective agent name is resolved."""
        self._session_agent[session_id] = agent_name
        logger.debug("Stats: session %s agent=%s", session_id[:8], agent_name)

    def record_session_start(self, session_id: str, agent_name: str) -> None:
        """Called when the agent begins execution."""
        self._session_start[session_id] = time.time()
        logger.debug("Stats: session %s started (agent=%s)", session_id[:8], agent_name)

    def record_tool_call(
        self, *, session_id: str, agent_name: str,
        step: int, tool_name: str,
        success: bool, duration_ms: float,
        tool_params: dict | None = None,
    ) -> None:
        """Called after each tool execution in the agent loop."""
        _truncated = _redacted_param_summary(dict(tool_params or {}))
        self._stats.record_step(
            session_id,
            step_number=step,
            tool_name=tool_name,
            tool_params=_truncated,
            status="success" if success else "error",
            duration_ms=int(duration_ms),
            tokens=0,
            timestamp="",
        )

    def record_context_snapshot(
        self,
        session_id: str,
        *,
        run_id: str = "",
        turn_id: str = "",
        step: int,
        request_kind: str,
        context_stats: Any,
        capabilities: dict[str, Any] | None = None,
    ) -> int:
        """Record context facts after request assembly and before provider I/O."""
        if context_stats is None:
            return 0
        stats = (
            asdict(context_stats)
            if is_dataclass(context_stats)
            else dict(context_stats)
        )
        return self._stats.record_context_snapshot(
            session_id,
            run_id=run_id,
            turn_id=turn_id,
            step_number=step,
            request_kind=request_kind,
            stats=stats,
            capabilities=dict(capabilities or {}),
        )

    def record_llm_turn(self, **payload: Any) -> int:
        return self._stats.record_llm_turn(payload)

    def record_session_end(
        self, session_id: str, *,
        agent_name: str,
        total_steps: int, total_tokens: int,
        status: str = "completed",
        completion_blocked: int = 0,
    ) -> None:
        """Called when the agent finishes. agent_name comes from the agent loop."""
        start = self._session_start.pop(session_id, None)
        duration_ms = int((time.time() - start) * 1000) if start else 0

        steps = self._stats.get_session_steps(session_id)
        tool_summary: dict[str, int] = {}
        for s in steps:
            tn = s.get("tool_name", "")
            if tn:
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

        _productive = total_steps - completion_blocked if completion_blocked else total_steps
        logger.info(
            "Stats finalized — session=%s steps=%d (productive=%d, blocked=%d) tokens=%d duration=%dms",
            session_id[:8], total_steps, _productive, completion_blocked, total_tokens, duration_ms,
        )
