"""
Approvals router — approve/reject pending plan proposals and worktree results.

Mounted under ``/api/sessions/{id}/approve`` and ``/api/sessions/{id}/reject``.

In the MVP, approvals are a placeholder for the Plan Mode approval loop.
Future iterations will wire these to the actual approval gate in
``hitl/pipeline.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from server.schemas.session import (
    ApprovalResponse,
    ApproveRequest,
    RejectRequest,
)

logger = logging.getLogger(__name__)


def create_approvals_router(get_service: Any) -> APIRouter:
    """Create the approvals router with dependency injection.

    Args:
        get_service: FastAPI dependency callable returning AgentService.

    Returns:
        APIRouter configured with approval endpoints.
    """
    router = APIRouter(tags=["approvals"])

    # ── POST /api/sessions/{session_id}/approve ──────────────────────────

    @router.post("/api/sessions/{session_id}/approve", response_model=ApprovalResponse)
    async def approve(
        session_id: str,
        body: ApproveRequest = ApproveRequest(),
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """
        Approve a pending plan proposal or worktree result.

        When an agent produces a plan (in plan mode) or a subagent worktree
        result, it may pause for human approval. This endpoint signals
        approval, allowing the agent to proceed.

        **Path Parameters:**
        - ``session_id`` (string): 12-char hex session ID.

        **Request Body:**
        - ``comment`` (string, optional): Approval comment.

        **Response (200):**
        - ``approved`` (bool): Always true for successful approval.
        - ``session_id`` (string): The session ID.
        - ``status`` (string): Updated session status.

        **Errors:**
        - 404: Session not found.
        - 501: Not implemented — approval routing not yet wired.

        **Implementation note (MVP):**
        This endpoint currently returns a placeholder response.  The full
        implementation will inject an approval message into the session's
        conversation history and resume the agent loop.
        """
        rec = service.session_service.get_session(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

        # TODO(MVP+1): Wire to actual approval gate
        #   1. Load the pending approval request from the session's metadata
        #   2. Inject "APPROVED" message into session history
        #   3. Resume agent execution via SessionRuntime.prepare_session_resume()
        logger.info("Approve called for session %s (comment: %s)", session_id, body.comment)

        return {
            "approved": True,
            "session_id": session_id,
            "status": "approved",
        }

    # ── POST /api/sessions/{session_id}/reject ───────────────────────────

    @router.post("/api/sessions/{session_id}/reject", response_model=ApprovalResponse)
    async def reject(
        session_id: str,
        body: RejectRequest,
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """
        Reject a pending plan proposal or worktree result.

        When an agent produces a plan or subagent worktree result, this
        endpoint signals rejection with a reason, allowing the agent to
        revise the approach.

        **Path Parameters:**
        - ``session_id`` (string): 12-char hex session ID.

        **Request Body:**
        - ``reason`` (string, required): Explanation for rejection.

        **Response (200):**
        - ``approved`` (bool): False for rejection.
        - ``session_id`` (string): The session ID.
        - ``status`` (string): Updated session status.

        **Errors:**
        - 404: Session not found.
        - 422: Missing rejection reason.
        - 501: Not implemented — approval routing not yet wired.
        """
        rec = service.session_service.get_session(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

        # TODO(MVP+1): Wire to actual rejection gate
        logger.info("Reject called for session %s (reason: %s)", session_id, body.reason)

        return {
            "approved": False,
            "session_id": session_id,
            "status": "rejected",
        }

    # ── GET /api/sessions/{session_id}/pending-approvals ─────────────────

    @router.get("/api/sessions/{session_id}/pending-approvals")
    async def list_pending_approvals(
        session_id: str,
        service=Depends(get_service),
    ) -> list[dict[str, Any]]:
        """
        List all pending approval requests for a session.

        **Path Parameters:**
        - ``session_id`` (string): 12-char hex session ID.

        **Response (200):**
        Array of pending approval items, each with:
        - ``type`` (string): 'plan_proposal' | 'worktree_result' | 'tool_approval'.
        - ``summary`` (string): Human-readable description.
        - ``created_at`` (string): ISO-8601 timestamp.

        **Errors:**
        - 404: Session not found.

        **Note (MVP):** Returns empty list — pending approval tracking
        requires the full approval gate integration.
        """
        rec = service.session_service.get_session(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

        # MVP: no pending approval tracking yet
        return []

    return router
