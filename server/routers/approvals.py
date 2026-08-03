"""
Approvals router — approve/reject pending plan proposals.

Mounted under ``/api/sessions/{id}/approve`` and ``/api/sessions/{id}/reject``.

After a plan agent finishes, the frontend receives a ``plan_ready`` WS event.
The user can approve (trigger build with plan context) or reject (re-run plan
with feedback).  Revision count is tracked in session metadata (capped at 5).
"""

from __future__ import annotations

import json
import hashlib
import logging
from datetime import datetime, timezone
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

    @router.post(
        "/api/sessions/{session_id}/approve",
        response_model=ApprovalResponse,
    )
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

        # CC-aligned: detect if user edited the plan file before approving
        plan_was_edited = False
        try:
            from pathlib import Path
            plan_file = Path(service.repo_path) / ".grace" / "plans" / f"{session_id}.md"
            if plan_file.is_file():
                # Compare current file hash to stored revision hash
                import hashlib
                current_hash = hashlib.sha256(
                    plan_file.read_text(encoding="utf-8").encode()
                ).hexdigest()
                if hasattr(service, '_plan_revisions'):
                    revs = service._plan_revisions.list_revisions(session_id)
                    if revs:
                        stored_hash = getattr(revs[-1], "content_hash", "")
                        if stored_hash and current_hash != stored_hash:
                            plan_was_edited = True
        except Exception:
            pass

        # CC-aligned: extract allowedPrompts from plan contract for build session
        plan_contract = rec.metadata.get("plan_contract", {}) if rec.metadata else {}
        allowed_prompts = list(plan_contract.get("allowed_prompts", []) or [])

        approval_tag = "Approved Plan (edited by user)" if plan_was_edited else "Approved Plan"
        plan_context = f"[PLAN CONTEXT] {approval_tag}. Execute it now."
        if comment:
            plan_context += f"\n\nApprover note: {comment}"

        # CC-aligned: inject structured contract steps so the build agent
        # has a concrete task list to work through (not just raw summary).
        _steps = plan_contract.get("steps", [])
        _targets = plan_contract.get("target_files", [])
        _verification = plan_contract.get("verification", "")
        if _steps or _targets:
            plan_context += "\n\n## Plan Steps"
            for i, step in enumerate(_steps, 1):
                plan_context += f"\n{i}. {step}"
            if _targets:
                plan_context += "\n\n## Target Files"
                for f in _targets:
                    plan_context += f"\n- {f}"
            if _verification:
                plan_context += f"\n\n## Verification\n{_verification}"
            plan_context += f"\n\n## Full Plan\n{plan_text}"
        else:
            plan_context += f"\n\n{plan_text}"

        submitted = _submit_plan_run(
            service,
            session_id=session_id,
            prompt=plan_context,
            action="approve",
            discriminator=f"{plan_text}\n{comment}",
        )
        if not submitted.created:
            return ApprovalResponse(
                approved=True,
                session_id=session_id,
                status="running",
                message="Build already submitted",
                run_id=submitted.run_id,
                turn_id=submitted.turn_id,
                turn_index=submitted.turn_index,
            )

        current_revision = _current_plan_revision(service, rec)

        # Mark plan revision as approved
        if hasattr(service, '_plan_revisions'):
            try:
                service._plan_revisions.mark_status(
                    session_id,
                    current_revision,
                    "approved",
                )
            except Exception:
                pass

        logger.info("Plan approved for session %s — starting build", session_id)
        # Update session agent_name and mark the phase transition.
        # Plan file is KEPT on disk so PlanView can reference it after approval.
        try:
            service.session_service.update_agent_name(session_id, "build")
            _transition_plan_metadata(
                service,
                session_id,
                marker="plan_approved_at",
                revision=current_revision,
                clear_contract=False,
            )
        except Exception:
            pass

        await _start_submitted_plan_run(
            service,
            submitted=submitted,
            session_id=session_id,
            prompt=plan_context,
            agent_name="build",
            intent="edit",
            allowed_prompts=allowed_prompts,
        )

        return ApprovalResponse(
            approved=True,
            session_id=session_id,
            status="running",
            message="Build started with plan context",
            run_id=submitted.run_id,
            turn_id=submitted.turn_id,
            turn_index=submitted.turn_index,
        )

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

        current_revision = _current_plan_revision(service, rec)
        if current_revision >= _MAX_PLAN_REVISIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Maximum plan revisions ({_MAX_PLAN_REVISIONS}) reached. Please approve or start a new plan.",
            )

        reason = body.reason.strip()

        feedback = (
            f"[PLAN REVISION REQUEST] The previous plan was rejected. "
            f"Please revise based on the following feedback:\n\n{reason}"
        )

        submitted = _submit_plan_run(
            service,
            session_id=session_id,
            prompt=feedback,
            action="reject",
            discriminator=f"{current_revision}\n{rec.summary or ''}\n{reason}",
        )
        if not submitted.created:
            return ApprovalResponse(
                approved=False,
                session_id=session_id,
                status="running",
                message=f"Revision {current_revision + 1}/{_MAX_PLAN_REVISIONS} already submitted",
                run_id=submitted.run_id,
                turn_id=submitted.turn_id,
                turn_index=submitted.turn_index,
            )

        # Mark the current revision rejected only after the idempotent Run/Turn
        # submission succeeds. A retried HTTP request must not append again.
        if hasattr(service, '_plan_revisions'):
            try:
                service._plan_revisions.mark_status(
                    session_id, current_revision, "rejected"
                )
            except Exception:
                pass

        # Clear stale lifecycle markers from any prior approve / save / abort
        # cycle so build_plan_state() sees this as a fresh "waiting" plan.
        _transition_plan_metadata(
            service,
            session_id,
            marker=None,
            revision=current_revision,
            clear_contract=True,
        )

        logger.info("Plan rejected for session %s (revision %d/%d) — re-running plan",
                     session_id, current_revision + 1, _MAX_PLAN_REVISIONS)

        # Ensure DB agent_name is "plan" for re-plan execution
        try:
            service.session_service.update_agent_name(session_id, "plan")
        except Exception:
            pass
        await _start_submitted_plan_run(
            service,
            submitted=submitted,
            session_id=session_id,
            prompt=feedback,
            agent_name="plan",
            intent="analysis",
        )

        return ApprovalResponse(
            approved=False,
            session_id=session_id,
            status="running",
            message=f"Revision {current_revision + 1}/{_MAX_PLAN_REVISIONS} started",
            run_id=submitted.run_id,
            turn_id=submitted.turn_id,
            turn_index=submitted.turn_index,
        )

    # ── POST /api/sessions/{session_id}/save-plan ─────────────────────────

    @router.post("/api/sessions/{session_id}/save-plan")
    async def save_plan(
        session_id: str,
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """
        Save a plan proposal without executing it.

        Marks the plan revision as saved and updates the session so it
        can be approved (built) later.  No background build is started.

        **Response (200):**
        - ``saved`` (bool): Always true.
        - ``session_id`` (string): The session ID.
        """
        rec = service.session_service.get_session(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

        plan_text = rec.summary
        if not plan_text or not plan_text.strip():
            raise HTTPException(status_code=400, detail="No plan found in session summary")

        current_revision = _current_plan_revision(service, rec)
        # Mark plan revision as saved + write metadata for PlanView recognition
        if hasattr(service, '_plan_revisions'):
            try:
                service._plan_revisions.mark_status(
                    session_id,
                    current_revision,
                    "saved",
                )
            except Exception:
                pass

        # Clear old lifecycle markers first, then set only "saved" so
        # build_plan_state() see this (and not a stale "approved").
        try:
            _transition_plan_metadata(
                service,
                session_id,
                marker="plan_saved_at",
                revision=current_revision,
                clear_contract=False,
            )
        except Exception:
            pass

        logger.info("Plan saved for session %s (build deferred)", session_id)
        return {"saved": True, "session_id": session_id, "message": "Plan saved — build deferred"}

    # ── POST /api/sessions/{session_id}/abort-plan ────────────────────────

    @router.post("/api/sessions/{session_id}/abort-plan")
    async def abort_plan(
        session_id: str,
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """
        Abort a plan proposal without requesting a revision.

        Discards the current plan and clears plan-related metadata.
        No re-plan or build is triggered.

        **Response (200):**
        - ``aborted`` (bool): Always true.
        - ``session_id`` (string): The session ID.
        """
        rec = service.session_service.get_session(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

        current_revision = _current_plan_revision(service, rec)
        # Mark plan revision as aborted + write metadata for PlanView recognition
        if hasattr(service, '_plan_revisions'):
            try:
                service._plan_revisions.mark_status(
                    session_id,
                    current_revision,
                    "aborted",
                )
            except Exception:
                pass

        # Write phase transition marker so build_plan_state() can
        # briefly show lifecycle="aborted" until the next plan cycle.
        # Clear old markers first, then set only the abort marker.
        try:
            _transition_plan_metadata(
                service,
                session_id,
                marker="plan_aborted_at",
                revision=current_revision,
                clear_contract=True,
            )
        except Exception:
            pass

        if hasattr(service, 'remove_plan_file'):
            service.remove_plan_file(session_id)
        logger.info("Plan aborted for session %s", session_id)
        return {"aborted": True, "session_id": session_id, "message": "Plan discarded"}

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

    # ── Plan revision endpoints ─────────────────────────────────────────

    @router.get("/api/sessions/{session_id}/plan-revisions")
    async def list_plan_revisions(
        session_id: str,
        service=Depends(get_service),
    ) -> list[dict[str, Any]]:
        """List all plan revisions for a session, oldest first."""
        if not hasattr(service, '_plan_revisions'):
            return []
        return service._plan_revisions.list_revisions(session_id)

    @router.get("/api/sessions/{session_id}/plan-revisions/{revision}")
    async def get_plan_revision(
        session_id: str,
        revision: int,
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """Get a specific plan revision."""
        if not hasattr(service, '_plan_revisions'):
            raise HTTPException(status_code=404, detail="Plan revision service not available")
        rev = service._plan_revisions.get_revision(session_id, revision)
        if rev is None:
            raise HTTPException(status_code=404, detail=f"Revision {revision} not found")
        return rev

    @router.get("/api/sessions/{session_id}/plan-revisions/{from_rev}/diff/{to_rev}")
    async def diff_plan_revisions(
        session_id: str,
        from_rev: int,
        to_rev: int,
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """Compute a line-level diff between two plan revisions."""
        if not hasattr(service, '_plan_revisions'):
            raise HTTPException(status_code=404, detail="Plan revision service not available")
        return service._plan_revisions.compute_diff(session_id, from_rev, to_rev)

    return router


# ── ToolApprovalBody schema ──────────────────────────────────────────────────


from pydantic import BaseModel, Field


class ToolApprovalBody(BaseModel):
    """Request body for ``POST /api/sessions/{id}/tool-approve``.

    CC control_response equivalent.
    """
    request_id: str = Field(description="Approval request ID from the WS event")
    decision: str = Field(description="'allow' or 'deny'")
    note: str = Field(default="", description="Optional feedback")
    always: bool = Field(default=False, description="Persist as 'Always Allow' rule")
    updated_input: dict[str, Any] | None = Field(
        default=None,
        description="Modified tool parameters (CC updatedInput equivalent)",
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _submit_plan_run(
    service,
    *,
    session_id: str,
    prompt: str,
    action: str,
    discriminator: str,
):
    """Atomically create an idempotent Run/Turn for a Plan transition."""
    from server.services.run_submission import (
        IdempotencyConflictError,
        RunAlreadyActiveError,
        submit_run_turn,
    )

    digest = hashlib.sha256(
        f"{session_id}\0{action}\0{discriminator}".encode("utf-8")
    ).hexdigest()[:32]
    # Phase A: inject native coordinator (same pattern as sessions.py router)
    coordinator = (
        getattr(service, '_native_components', None)
        and service._native_components.run_coordinator
    ) if service is not None else None
    try:
        return submit_run_turn(
            service._storage,
            session_id=session_id,
            prompt=prompt,
            idempotency_key=f"plan:{action}:{digest}",
            coordinator=coordinator,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except RunAlreadyActiveError:
        raise HTTPException(status_code=409, detail="RUN_ALREADY_ACTIVE")


async def _start_submitted_plan_run(
    service,
    *,
    submitted,
    session_id: str,
    prompt: str,
    agent_name: str,
    intent: str,
    allowed_prompts: list[dict[str, str]] | None = None,
) -> None:
    """Start a previously persisted Plan transition with its RunContext."""
    if hasattr(service, "_event_bus") and service._event_bus is not None:
        await service._event_bus.create_session(session_id)

    from agent.session.models import PipelineRunContext as RunContext

    context = RunContext(
        session_id=session_id,
        run_id=submitted.run_id,
        turn_id=submitted.turn_id,
        turn_index=submitted.turn_index,
        idempotency_key="",
    )
    try:
        service.run_chat_async(
            session_id=session_id,
            prompt=prompt,
            agent_name=agent_name,
            intent=intent,
            allowed_prompts=allowed_prompts,
            run_context=context,
        )
    except Exception as exc:
        service._store.finalize_run_with_event(
            submitted.run_id,
            session_id,
            status="failed",
            error=str(exc),
            expect_status="queued",
        )
        raise HTTPException(status_code=409, detail=str(exc))

def _clear_plan_metadata(service, session_id: str) -> None:
    """Remove ALL plan lifecycle markers so a new plan starts fresh.

    Called before setting a new marker (approve / save / abort)
    and when a plan is discarded.  Must remove every key that
    ``build_plan_state()`` checks so stale markers cannot leak
    into a subsequent plan cycle on the same session.
    """
    try:
        store = service._storage.store
        with store._connect() as conn:
            rec = store.get_session(session_id)
            if rec is None:
                return
            meta = dict(rec.metadata)
            # All keys that control build_plan_state() lifecycle.
            for key in (
                "plan_revision", "plan_approved_at",
                "plan_saved_at", "plan_aborted_at",
                "plan_contract",
            ):
                meta.pop(key, None)
            conn.execute(
                "UPDATE sessions SET metadata_json = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=True), session_id),
            )
    except Exception:
        logger.exception("Failed to clear plan metadata for %s", session_id)


def _current_plan_revision(service, rec) -> int:
    """Resolve the current revision from durable revisions, then metadata."""
    revision = int((rec.metadata or {}).get("plan_revision", 0))
    if hasattr(service, "_plan_revisions"):
        try:
            revisions = service._plan_revisions.list_revisions(rec.id)
            if revisions:
                latest = revisions[-1]
                durable = int(
                    latest.get("revision", 0)
                    if isinstance(latest, dict)
                    else getattr(latest, "revision", 0)
                )
                revision = max(revision, durable)
        except Exception:
            pass
    if revision == 0 and (rec.summary or "").strip():
        return 1
    return revision


def _transition_plan_metadata(
    service,
    session_id: str,
    *,
    marker: str | None,
    revision: int,
    clear_contract: bool,
) -> None:
    """Atomically replace lifecycle markers while preserving plan facts."""
    try:
        store = service._storage.store
        with store._connect() as conn:
            rec = store.get_session(session_id)
            if rec is None:
                return
            meta = dict(rec.metadata)
            for key in (
                "plan_approved_at", "plan_saved_at", "plan_aborted_at",
            ):
                meta.pop(key, None)
            if marker:
                meta[marker] = datetime.now(timezone.utc).isoformat()
            if revision:
                meta["plan_revision"] = revision
            if clear_contract:
                meta.pop("plan_contract", None)
            conn.execute(
                "UPDATE sessions SET metadata_json = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=True), session_id),
            )
    except Exception:
        logger.exception("Failed to transition plan metadata for %s", session_id)
        raise
