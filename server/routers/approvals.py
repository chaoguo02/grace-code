"""
Approvals router — approve/reject pending plan proposals.

Mounted under ``/api/sessions/{id}/approve`` and ``/api/sessions/{id}/reject``.

After a plan agent finishes, the frontend receives a ``plan_ready`` WS event.
The user can approve (trigger build with plan context) or reject (re-run plan
with feedback).  Revision count is tracked in session metadata (capped at 5).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from server.schemas.session import (
    ApprovalResponse,
    ApproveRequest,
    RejectRequest,
)

logger = logging.getLogger(__name__)

_MAX_PLAN_REVISIONS = 5


def create_approvals_router(get_service: Any) -> APIRouter:
    """Create the approvals router with dependency injection."""
    router = APIRouter(tags=["approvals"])

    # ── POST /api/sessions/{session_id}/approve ──────────────────────────

    @router.post("/api/sessions/{session_id}/approve")
    async def approve(
        session_id: str,
        body: ApproveRequest = ApproveRequest(),
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """
        Approve a plan proposal and trigger the build execution.

        Reads the plan text from the session's summary, injects it as
        ``[PLAN CONTEXT]`` into the conversation, and starts a build agent
        run on the same session (preserving context continuity).

        **Response (200):**
        - ``approved`` (bool): Always true.
        - ``session_id`` (string): The session ID.
        - ``message`` (string): Status description.

        **Errors:**
        - 404: Session not found.
        - 400: Session has no plan to approve.
        """
        rec = service.session_service.get_session(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

        plan_text = rec.summary
        if not plan_text or not plan_text.strip():
            raise HTTPException(status_code=400, detail="No plan found in session summary")

        comment = body.comment.strip()
        plan_context = (
            f"[PLAN CONTEXT] The following implementation plan has been reviewed and approved. "
            f"Execute it now."
        )
        if comment:
            plan_context += f"\n\nApprover note: {comment}"
        plan_context += f"\n\n{plan_text}"

        from llm.base import LLMMessage
        service._storage.append_message(session_id, LLMMessage(
            role="user", content=plan_context,
        ))

        # Update metadata to clear plan state
        _clear_plan_metadata(service, session_id)

        logger.info("Plan approved for session %s — starting build", session_id)
        service.run_chat_async(
            session_id=session_id,
            prompt=plan_context,
            agent_name="build",
            intent="edit",
        )

        return {"approved": True, "session_id": session_id, "message": "Build started with plan context"}

    # ── POST /api/sessions/{session_id}/reject ───────────────────────────

    @router.post("/api/sessions/{session_id}/reject", response_model=ApprovalResponse)
    async def reject(
        session_id: str,
        body: RejectRequest,
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """
        Reject a plan proposal and request a revision.

        The rejection reason is fed back to the plan agent, which re-runs
        to produce a revised plan.  Maximum 5 revisions before requiring
        explicit approval.

        **Response (200):**
        - ``approved`` (bool): False.
        - ``session_id`` (string): The session ID.
        - ``message`` (string): Status description.

        **Errors:**
        - 404: Session not found.
        - 400: Max revisions reached.
        """
        rec = service.session_service.get_session(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

        rev_count = rec.metadata.get("plan_revision", 0)
        if rev_count >= _MAX_PLAN_REVISIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Maximum plan revisions ({_MAX_PLAN_REVISIONS}) reached. Please approve or start a new plan.",
            )

        reason = body.reason.strip()
        feedback = (
            f"[PLAN REVISION REQUEST] The previous plan was rejected. "
            f"Please revise based on the following feedback:\n\n{reason}"
        )

        from llm.base import LLMMessage
        service._storage.append_message(session_id, LLMMessage(
            role="user", content=feedback,
        ))

        # Increment revision counter
        _update_plan_revision(service, session_id, rev_count + 1)

        logger.info("Plan rejected for session %s (revision %d/%d) — re-running plan",
                     session_id, rev_count + 1, _MAX_PLAN_REVISIONS)

        service.run_chat_async(
            session_id=session_id,
            prompt=feedback,
            agent_name=rec.agent_name,  # Use the same agent that created the plan
            intent="analysis",
        )

        return {"approved": False, "session_id": session_id, "message": f"Revision {rev_count + 1}/{_MAX_PLAN_REVISIONS} started"}

    # ── GET /api/sessions/{session_id}/pending-approvals ─────────────────

    @router.get("/api/sessions/{session_id}/pending-approvals")
    async def list_pending_approvals(
        session_id: str,
        service=Depends(get_service),
    ) -> list[dict[str, Any]]:
        """
        List pending approval requests for a session.

        Returns plan proposals that are waiting for user approval.
        """
        rec = service.session_service.get_session(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

        # Check if this session has a plan waiting for approval
        if rec.agent_name == "plan" and rec.summary:
            return [{
                "type": "plan_proposal",
                "summary": rec.summary[:200],
                "agent_name": rec.agent_name,
                "revision": rec.metadata.get("plan_revision", 0),
                "created_at": rec.updated_at,
            }]
        return []

    return router


# ── Helpers ──────────────────────────────────────────────────────────────────

def _clear_plan_metadata(service, session_id: str) -> None:
    """Clear plan-related metadata from a session."""
    try:
        store = service._storage.store
        with store._connect() as conn:
            rec = store.get_session(session_id)
            if rec is None:
                return
            meta = dict(rec.metadata)
            meta.pop("plan_revision", None)
            conn.execute(
                "UPDATE sessions SET metadata_json = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=True), session_id),
            )
    except Exception:
        logger.exception("Failed to clear plan metadata for %s", session_id)


def _update_plan_revision(service, session_id: str, count: int) -> None:
    """Update the plan revision counter in session metadata."""
    try:
        store = service._storage.store
        with store._connect() as conn:
            rec = store.get_session(session_id)
            if rec is None:
                return
            meta = dict(rec.metadata)
            meta["plan_revision"] = count
            conn.execute(
                "UPDATE sessions SET metadata_json = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=True), session_id),
            )
    except Exception:
        logger.exception("Failed to update plan revision for %s", session_id)
