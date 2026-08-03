"""
Diffs router — code review endpoints for session file diffs.

Mounted under ``/api/diffs``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from server.schemas.stats import UpdateDiffRequest

logger = logging.getLogger(__name__)


def create_diffs_router(get_service: Any) -> APIRouter:
    """Create the diffs router with dependency injection."""
    router = APIRouter(prefix="/api/diffs", tags=["diffs"])

    def _ss(service):
        return service._stats_service

    # ── GET /api/diffs/pending ─────────────────────────────────────────

    @router.get("/pending")
    async def list_pending_diffs(
        service=Depends(get_service),
    ) -> list[dict]:
        """
        List all diffs pending review across all sessions.

        **Response (200):** Array of unreviewed diffs with session info.
        """
        sessions = service.session_service.list_sessions(limit=50)
        result = []
        for s in sessions:
            diffs = _ss(service).get_session_diffs(s["id"], status="pending")
            for d in diffs:
                d["session_title"] = s.get("title", "")
                d["session_agent"] = s.get("agent_name", "")
                result.append(d)
        return result

    # ── PATCH /api/diffs/{diff_id} ─────────────────────────────────────

    @router.patch("/{diff_id}")
    async def update_diff(
        diff_id: int,
        body: UpdateDiffRequest,
        service=Depends(get_service),
    ) -> dict:
        """
        Approve or reject a diff with an optional comment.

        **Path Parameters:**
        - ``diff_id`` (int): Diff row ID.

        **Request Body:**
        - ``status`` (string): ``"approved"`` or ``"rejected"``.
        - ``comment`` (string, optional): Review feedback.

        **Response (200):**
        - ``updated`` (bool): True if the diff was found and updated.
        """
        ok = _ss(service).update_diff_status(diff_id, body.status, body.comment)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Diff not found: {diff_id}")
        return {"updated": True, "status": body.status}

    return router
