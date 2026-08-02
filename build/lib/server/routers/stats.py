"""
Stats router — execution statistics and daily rollups.

Mounted under ``/api/stats``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

logger = logging.getLogger(__name__)


def create_stats_router(get_service: Any) -> APIRouter:
    """Create the stats router with dependency injection."""
    router = APIRouter(prefix="/api/stats", tags=["stats"])

    def _ss(service):
        return service._stats_service

    # ── GET /api/stats/sessions ─────────────────────────────────────────

    @router.get("/sessions")
    async def list_session_stats(
        days: int = 7,
        status: str | None = None,
        service=Depends(get_service),
    ) -> list[dict]:
        """
        List aggregate stats for recent sessions.

        **Query Parameters:**
        - ``days`` (int, default 7): Look back period.
        - ``status`` (str, optional): Filter by status.

        **Response (200):** Array of session stats.
        """
        # Get recent sessions
        sessions = service.session_service.list_sessions(limit=100)
        result = []
        for s in sessions:
            stats = _ss(service).get_session_stats(s["id"])
            if stats:
                if status and stats.get("status") != status:
                    continue
                result.append(stats)
        return result

    # ── GET /api/stats/daily ───────────────────────────────────────────

    @router.get("/daily")
    async def get_daily_rollups(
        days: int = 30,
        service=Depends(get_service),
    ) -> list[dict]:
        """
        Get daily aggregate stats for charts.

        **Query Parameters:**
        - ``days`` (int, default 30): Number of days to return.

        **Response (200):** Array of daily rollups.
        """
        return _ss(service).get_daily_rollups(days=days)

    # ── GET /api/stats/tools ───────────────────────────────────────────

    @router.get("/tools")
    async def get_tool_rankings(
        days: int = 7,
        service=Depends(get_service),
    ) -> dict[str, int]:
        """
        Get aggregated tool usage counts.

        **Query Parameters:**
        - ``days`` (int, default 7): Look back period.

        **Response (200):** ``{"Read": 15, "Edit": 8, "Bash": 3}``
        """
        # Aggregate from session_stats
        sessions = service.session_service.list_sessions(limit=100)
        merged: dict[str, int] = {}
        for s in sessions:
            stats = _ss(service).get_session_stats(s["id"])
            if stats:
                tools_raw = stats.get("tool_summary", {})
                # Handle both pre-parsed dict (StatsService) and raw JSON string (legacy)
                if isinstance(tools_raw, str):
                    import json
                    tools_raw = json.loads(tools_raw) if tools_raw.strip() else {}
                tools: dict = tools_raw if isinstance(tools_raw, dict) else {}
                for tool, count in tools.items():
                    merged[tool] = merged.get(tool, 0) + count
        return dict(sorted(merged.items(), key=lambda x: -x[1]))

    @router.get("/llm")
    async def get_llm_metrics(
        session_id: str = "",
        limit: int = 200,
        offset: int = 0,
        service=Depends(get_service),
    ) -> dict:
        rows = _ss(service).get_llm_turns(session_id, limit=limit, offset=offset)
        return {
            "overview": _ss(service).llm_overview(rows),
            "items": rows,
            "limit": max(1, min(limit, 1000)),
            "offset": max(0, offset),
        }

    return router
