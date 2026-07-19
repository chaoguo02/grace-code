"""
Sessions router — session lifecycle and chat execution.

All endpoints are mounted under ``/api/sessions``.

Every endpoint includes a complete request/response documentation block
at the top of its handler function.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from server.routers.approvals import ToolApprovalBody
from server.services.event_bus import _translate_event
from server.schemas.session import (
    CancelRequest,
    CancelResponse,
    ChatRequest,
    ChatResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    SessionDetail,
    SessionSummary,
    MessageResponse,
    BatchDeleteRequest,
    BatchDeleteResponse,
    UpdateSessionRequest,
    UpdateSessionResponse,
    ModelSwitchRequest,
)

logger = logging.getLogger(__name__)


def create_sessions_router(get_service: Any) -> APIRouter:
    """Create the sessions router with dependency injection.

    Args:
        get_service: FastAPI dependency callable returning AgentService.

    Returns:
        APIRouter configured with all session endpoints.
    """
    router = APIRouter(prefix="/api/sessions", tags=["sessions"])

    # ── Helper: session record → summary dict ────────────────────────────

    def _summary_from_record(rec) -> dict[str, Any]:
        return {
            "id": rec.id,
            "agent_name": rec.agent_name,
            "title": rec.title,
            "status": rec.status.value if hasattr(rec.status, "value") else rec.status,
            "mode": rec.mode.value if hasattr(rec.mode, "value") else rec.mode,
            "summary": rec.summary,
            "error": rec.error,
            "parent_id": rec.parent_id,
            "created_at": rec.created_at,
            "updated_at": rec.updated_at,
            "completed_at": rec.completed_at,
        }

    def _detail_from_record(rec) -> dict[str, Any]:
        return {
            "id": rec.id,
            "parent_id": rec.parent_id,
            "root_id": rec.root_id,
            "agent_name": rec.agent_name,
            "title": rec.title,
            "status": rec.status.value if hasattr(rec.status, "value") else rec.status,
            "mode": rec.mode.value if hasattr(rec.mode, "value") else rec.mode,
            "summary": rec.summary,
            "error": rec.error,
            "agent_kind": rec.agent_kind.value if hasattr(rec.agent_kind, "value") else rec.agent_kind,
            "context_origin": rec.context_origin.value if hasattr(rec.context_origin, "value") else rec.context_origin,
            "execution_placement": rec.execution_placement.value if hasattr(rec.execution_placement, "value") else rec.execution_placement,
            "workspace_mode": rec.workspace_mode.value if hasattr(rec.workspace_mode, "value") else rec.workspace_mode,
            "agent_depth": rec.agent_depth.value if hasattr(rec.agent_depth, "value") else int(rec.agent_depth),
            "generation": rec.generation,
            "created_at": rec.created_at,
            "updated_at": rec.updated_at,
            "completed_at": rec.completed_at,
            "metadata": rec.metadata,
        }

    # ── POST /api/sessions ───────────────────────────────────────────────

    @router.post("", response_model=CreateSessionResponse)
    async def create_session(
        body: CreateSessionRequest,
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """
        Create a new root session.

        Creates a root session in the SessionStore. The session starts in
        ``queued`` status and can be executed via ``POST /api/sessions/{id}/chat``.

        **Request Body:**
        - ``agent_name`` (string, default 'build'): Agent definition to use.
        - ``repo_path`` (string, required): Absolute repo path.
        - ``title`` (string, optional): Session title.

        **Response (201):**
        - ``session_id`` (string): 12-char hex session ID.
        - ``agent_name`` (string): Agent used.
        - ``status`` (string): 'queued'.
        - ``repo_path`` (string): Repository path.
        - ``created_at`` (string): ISO-8601 timestamp.

        **Errors:**
        - 422: Validation error (missing repo_path, invalid agent_name).
        - 500: Internal error creating the session.
        """
        try:
            session_id = service.create_session(
                agent_name=body.agent_name,
                repo_path=body.repo_path,
                title=body.title,
            )
            rec = service.session_service.get_session(session_id)
            if rec is None:
                raise HTTPException(status_code=500, detail="Session was created but could not be found")
            return {
                "session_id": rec.id,
                "agent_name": rec.agent_name,
                "status": rec.status.value if hasattr(rec.status, "value") else rec.status,
                "repo_path": rec.repo_path,
                "created_at": rec.created_at,
            }
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:
            logger.exception("Failed to create session")
            raise HTTPException(status_code=500, detail=f"Internal error: {exc}")

    # ── GET /api/sessions ────────────────────────────────────────────────

    @router.get("")
    async def list_sessions(
        limit: int = 50,
        offset: int = 0,
        service=Depends(get_service),
    ) -> list[dict[str, Any]]:
        """
        List all sessions.

        Returns sessions ordered by most recently updated (descending).

        **Query Parameters:**
        - ``limit`` (int, default 50): Maximum sessions to return.
        - ``offset`` (int, default 0): Pagination offset.

        **Response (200):**
        Array of session summaries, each with:
        - ``id`` (string): Session ID.
        - ``agent_name`` (string): Agent definition.
        - ``title`` (string): Session title.
        - ``status`` (string): Current status.
        - ``mode`` (string): Session mode.
        - ``summary`` (string): Result summary.
        - ``error`` (string): Error text.
        - ``parent_id`` (string|null): Parent session ID.
        - ``created_at`` (string): Creation timestamp.
        - ``updated_at`` (string): Last update timestamp.
        - ``completed_at`` (string|null): Completion timestamp.

        **Errors:**
        - 500: Internal error querying sessions.
        """
        try:
            return service.session_service.list_sessions(limit=limit, offset=offset)
        except Exception as exc:
            logger.exception("Failed to list sessions")
            raise HTTPException(status_code=500, detail=str(exc))

    # ── GET /api/sessions/{session_id} ───────────────────────────────────

    @router.get("/{session_id}", response_model=SessionDetail)
    async def get_session(
        session_id: str,
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """
        Get full session details.

        **Path Parameters:**
        - ``session_id`` (string): 12-char hex session ID.

        **Response (200):**
        Full session record with all fields including agent_kind, context_origin,
        execution_placement, workspace_mode, agent_depth, generation, metadata.

        **Errors:**
        - 404: Session not found.
        """
        rec = service.session_service.get_session_detail(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        return rec

    # ── GET /api/sessions/{session_id}/messages ──────────────────────────

    @router.get("/{session_id}/messages", response_model=list[MessageResponse])
    async def get_session_messages(
        session_id: str,
        service=Depends(get_service),
    ) -> list[dict[str, Any]]:
        """
        Get all messages for a session.

        Messages are persisted conversation history between user and agent,
        ordered by creation time (oldest first).

        **Path Parameters:**
        - ``session_id`` (string): 12-char hex session ID.

        **Response (200):**
        Array of messages, each with:
        - ``role`` (string): 'user' | 'assistant' | 'tool'.
        - ``content`` (string): Message text.
        - ``tool_calls`` (list|null): Tool invocations (assistant only).
        - ``tool_call_id`` (string|null): Tool call reference (tool only).
        - ``tool_name`` (string|null): Tool name (tool only).

        **Errors:**
        - 404: Session not found.
        """
        try:
            return service.session_service.get_messages(session_id)
        except ValueError:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    # ── GET /api/sessions/{session_id}/events ────────────────────────────

    @router.get("/{session_id}/events")
    async def get_session_events(
        session_id: str,
        after: int = 0,
        limit: int = 1000,
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """
        Get execution events for a session.

        Events are the structured record of the ReAct loop: every action,
        observation, reflection, and lifecycle event.  They are read from
        the agent's JSONL event log files on disk.

        **Path Parameters:**
        - ``session_id`` (string): 12-char hex session ID.

        **Query Parameters:**
        - ``after`` (int, default 0): Skip this many events from the start.
        - ``limit`` (int, default 1000): Max events to return.

        **Response (200):**
        - ``events`` (array): Event objects with ``event_id``, ``event_type``,
          ``task_id``, ``timestamp``, ``payload``.
        - ``total`` (int): Total events found.
        - ``has_more`` (bool): Whether more events exist beyond this page.

        **Errors:**
        - 404: Session not found.
        """
        try:
            events = service.session_service.get_events(
                session_id, after=after, limit=limit,
            )
            return {
                "events": events,
                "total": len(events),
                "has_more": len(events) >= limit,
            }
        except ValueError:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    # ── GET /api/sessions/{session_id}/trace/events ──────────────────────
    # Returns events in WebSocket message format (pre-translated).

    @router.get("/{session_id}/trace/events")
    async def get_session_trace_events(
        session_id: str,
        after: int = 0,
        limit: int = 200,
        service=Depends(get_service),
    ) -> list[dict]:
        """
        Get session execution events pre-translated to WS message format.

        Unlike ``GET /events`` which returns raw EventLog payloads, this
        endpoint returns events in the same format as the WebSocket stream
        (``thought``, ``tool_call``, ``observation``, etc.) — ready for
        the frontend timeline to consume directly.

        **Response (200):** Array of WS-format event objects.
        """
        try:
            raw = service.session_service.get_events(
                session_id, after=after, limit=limit,
            )
        except ValueError:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

        from agent.task import Event
        result: list[dict] = []
        for ev in raw:
            # Wrap raw event as an Event-like object for _translate_event
            class _FakeEvent:
                event_type = ev.get("event_type", "")
                payload = ev.get("payload", {})
                timestamp = ev.get("timestamp", "")
                event_id = ev.get("event_id", "")
                task_id = ev.get("task_id", "")
                session_id = session_id
            try:
                msgs = _translate_event(_FakeEvent())
                result.extend(msgs)
            except Exception:
                pass
        return result

    # ── POST /api/sessions/{session_id}/messages ──────────────────────────
    #
    # ═══════════════════════════════════════════════════════════════════════
    #  CORE ENDPOINT — Send a message to trigger the ReAct agent loop
    # ═══════════════════════════════════════════════════════════════════════

    @router.post("/{session_id}/messages", status_code=202)
    async def create_message(
        session_id: str,
        body: ChatRequest,
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """
        Send a message to the session.  Triggers the ReAct agent loop.

        This is the **core endpoint** — it starts the agent execution in a
        background thread and returns immediately with ``202 Accepted``.
        All execution events are streamed in real-time through WebSocket
        at ``/api/ws/sessions/{session_id}``.

        **How it works:**
        1. Validates the session and returns 202 immediately.
        2. The agent execution runs in a background thread.
        3. Connect a WebSocket to ``/api/ws/sessions/{session_id}`` **before**
           calling this endpoint to receive real-time events.
        4. Events arrive in this order:
           - ``{"type": "status", "status": "running"}`` — execution started
           - ``{"type": "thought", "content": "..."}`` — model thinking
           - ``{"type": "tool_call", "name": "Read", ...}`` — tool invoked
           - ``{"type": "observation", "tool_name": "Read", ...}`` — tool result
           - ``{"type": "status", "status": "completed", "result": {...}}`` — done
        5. After completion, ``GET /api/sessions/{id}/messages`` has the
           full conversation history.

        **Request Body:**
        - ``prompt`` (string, required): User's task description.
        - ``agent_name`` (string|null, default null): Override agent definition.
        - ``intent`` (string|null, default null): ``'edit'`` | ``'analysis'``.

        **Response (202):**
        - ``session_id`` (string): The session ID.
        - ``status`` (string): ``'running'`` — execution has started.

        **Errors:**
        - 404: Session not found.
        - 422: Validation error (empty prompt).
        """
        rec = service.session_service.get_session(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

        effective_agent = body.agent_name or rec.agent_name

        # Update session agent_name if the effective agent differs.
        # This keeps the session record consistent when the user switches
        # mode mid-session (e.g. build → plan via intent=analysis).
        if effective_agent != rec.agent_name:
            try:
                service.session_service.update_agent_name(session_id, effective_agent)
            except Exception:
                pass

        # Ensure event bus subscriber exists
        if hasattr(service, "_event_bus") and service._event_bus is not None:
            await service._event_bus.create_session(session_id)

        # Auto-title: if the session has an auto-generated title,
        # update it to the first 50 chars of the first prompt
        if rec.title.startswith("Session ") and body.prompt.strip():
            try:
                new_title = body.prompt.strip()[:60]
                service.session_service.update_title(session_id, new_title)
            except Exception:
                pass

        # Start async execution in background thread
        service.run_chat_async(
            session_id=session_id,
            prompt=body.prompt,
            agent_name=effective_agent,
            intent=body.intent,
        )

        return {"accepted": True}

    # ── POST /api/sessions/{session_id}/cancel ───────────────────────────

    @router.post("/{session_id}/cancel", response_model=CancelResponse)
    async def cancel_session(
        session_id: str,
        body: CancelRequest = CancelRequest(),
        service=Depends(get_service),
    ) -> dict[str, bool]:
        """
        Cancel a running session.

        Sends a cancellation signal to the session's execution thread.
        The agent will stop at the next safe point (tool boundary or
        LLM call).

        **Path Parameters:**
        - ``session_id`` (string): 12-char hex session ID.

        **Request Body:**
        - ``detail`` (string, optional): Cancellation reason.

        **Response (200):**
        - ``cancelled`` (bool): True if cancellation signal was sent.

        **Note:** Returns ``cancelled: true`` even if the session was already
        completed (idempotent).  Returns ``cancelled: false`` only if the
        session has no active cancellation token registered.
        """
        try:
            result = service.cancel_session(session_id, detail=body.detail)
            return {"cancelled": result}
        except Exception as exc:
            logger.exception("Failed to cancel session %s", session_id)
            return {"cancelled": False}

    # ── PATCH /api/sessions/{session_id} ─────────────────────────────────

    @router.patch("/{session_id}", response_model=UpdateSessionResponse)
    async def update_session(
        session_id: str,
        body: UpdateSessionRequest,
        service=Depends(get_service),
    ) -> dict:
        """
        Update session attributes (e.g. switch agent mode).

        **Path Parameters:**
        - ``session_id`` (string): 12-char hex session ID.

        **Request Body:**
        - ``agent_name`` (string|null): New agent name, e.g. ``"plan"``.

        **Response (200):**
        - ``updated`` (bool): True if any field was updated.
        - ``agent_name`` (string|null): The new agent name, if changed.

        **Errors:**
        - 404: Session not found.
        """
        rec = service.session_service.get_session(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

        updated = False
        new_agent_name = None
        if body.agent_name is not None and body.agent_name != rec.agent_name:
            ok = service.session_service.update_agent_name(session_id, body.agent_name)
            if ok:
                updated = True
                new_agent_name = body.agent_name

        return {"updated": updated, "agent_name": new_agent_name}

    # ── POST /api/sessions/{session_id}/compact ──────────────────────────

    @router.post("/{session_id}/compact", status_code=202)
    async def compact_session(
        session_id: str,
        service=Depends(get_service),
    ) -> dict:
        """
        Trigger context compression for a session.

        Runs the compression pipeline (Snip → MicroCompact → AutoCompact)
        in a background thread. Results are pushed via WebSocket.

        **Path Parameters:**
        - ``session_id`` (string): 12-char hex session ID.

        **Response (202):**
        - ``accepted`` (bool): True if compaction was scheduled.
        """
        rec = service.session_service.get_session(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

        service.compact_session_async(session_id)
        return {"accepted": True}

    # ── GET /api/sessions/{session_id}/diff ───────────────────────────────

    @router.get("/{session_id}/diff")
    async def get_session_diff(
        session_id: str,
        service=Depends(get_service),
    ) -> dict:
        """
        Get git diff of files modified during this session.

        Shows the unified diff of all unstaged changes in the repo.
        For per-file diffs in real time, see the ``diff`` field on
        ``observation`` WS events for Edit/Write tools.

        **Path Parameters:**
        - ``session_id`` (string): 12-char hex session ID.

        **Response (200):**
        - ``diff`` (string): Unified git diff output.
        - ``has_diff`` (bool): True if there are uncommitted changes.
        """
        import subprocess
        repo = service.repo_path
        try:
            result = subprocess.run(
                ["git", "diff"],
                capture_output=True, text=True,
                cwd=repo, timeout=10,
            )
            raw = result.stdout.strip()
            return {"diff": raw, "has_diff": bool(raw)}
        except Exception as exc:
            return {"diff": "", "has_diff": False, "error": str(exc)}

    # ── POST /api/sessions/batch-delete ────────────────────────────────────
    # POST (not DELETE) because some HTTP clients strip DELETE request bodies.

    @router.post("/batch-delete", response_model=BatchDeleteResponse)
    async def delete_sessions_batch(
        body: BatchDeleteRequest,
        service=Depends(get_service),
    ) -> dict[str, int]:
        """
        Permanently delete multiple sessions and all their messages.

        **Request Body:**
        - ``session_ids`` (list[str]): Session IDs to delete.

        **Response (200):**
        - ``deleted_count`` (int): Number of sessions actually deleted.
        - ``total_requested`` (int): Number of IDs sent.

        Non-existent IDs are silently skipped.
        """
        deleted = service.session_service.delete_sessions_batch(body.session_ids)
        return {"deleted_count": deleted, "total_requested": len(body.session_ids)}

    # ── DELETE /api/sessions/{session_id} ──────────────────────────────

    @router.delete("/{session_id}")
    async def delete_session(
        session_id: str,
        service=Depends(get_service),
    ) -> dict[str, bool]:
        """
        Permanently delete a session and all its messages.

        **Path Parameters:**
        - ``session_id`` (string): 12-char hex session ID.

        **Response (200):**
        - ``deleted`` (bool): True if the session was found and deleted.

        **Errors:**
        - 404: Session not found.
        """
        rec = service.session_service.get_session(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        deleted = service.session_service.delete_session(session_id)
        return {"deleted": deleted}

    # ── POST /api/sessions/{session_id}/settings ───────────────────────
    # Update session runtime settings (effort, thinking, permission mode).

    @router.post("/{session_id}/settings")
    async def update_session_settings(
        session_id: str,
        body: dict[str, Any],
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """Update runtime settings for the session (next-message effective)."""
        rec = service.session_service.get_session(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

        changed: list[str] = []
        if "effort" in body:
            service._runtime.set_pending_effort(session_id, body["effort"])
            changed.append("effort")
        if "thinking" in body and isinstance(body["thinking"], bool):
            service._runtime.set_pending_thinking(session_id, body["thinking"])
            changed.append("thinking")
        if "permission_mode" in body:
            service._runtime.set_pending_permission_mode_override(session_id, body["permission_mode"])
            changed.append("permission_mode")

        return {"updated": changed, "session_id": session_id}

    # ── POST /api/sessions/{session_id}/model ──────────────────────────
    # Switch the LLM model mid-session.  Takes effect on the next message.

    @router.post("/{session_id}/model")
    async def switch_model(
        session_id: str,
        body: "ModelSwitchRequest",
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """Switch the LLM model for an active session (next-message effective)."""

        rec = service.session_service.get_session(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

        # Store pending model switch — applied in run_chat_async before next run
        service._runtime.set_pending_model(session_id, body.model, body.provider)
        logger.info("Model switch queued — session=%s model=%s provider=%s",
                     session_id[:8], body.model, body.provider)

        return {"switched": True, "session_id": session_id, "model": body.model}

    # ── POST /api/sessions/{session_id}/tool-approve ────────────────────
    # CC control_response equivalent.  Moved here (sessions router with
    # /api/sessions prefix) because approvals router has no prefix and
    # FastAPI path matching fails for absolute paths on un-prefixed routers.

    @router.post("/{session_id}/tool-approve")
    async def approve_tool(
        session_id: str,
        body: "ToolApprovalBody",
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """Resolve a pending tool approval (CC control_response equivalent)."""
        from hitl.pipeline import PromptAction, PromptDecision

        broker = service._runtime.get_approval_broker(session_id)
        if broker is None:
            raise HTTPException(status_code=404, detail=f"No active approval broker for session {session_id}")

        if body.decision == "allow":
            action = PromptAction.ALWAYS_ALLOW if body.always else PromptAction.ALLOW_ONCE
        else:
            action = PromptAction.DENY

        decision = PromptDecision(
            action=action, note=body.note or "",
            updated_params=body.updated_input,
        )
        ok = broker.resolve(body.request_id, decision)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Approval request {body.request_id} not found (may have timed out)")

        return {"resolved": True}

    # ── GET /api/sessions/{session_id}/stats ───────────────────────────

    @router.get("/{session_id}/stats")
    async def get_session_stats(
        session_id: str,
        service=Depends(get_service),
    ) -> dict:
        """Get aggregate execution stats for a session."""
        rec = service.session_service.get_session(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        stats = service._stats_service.get_session_stats(session_id)
        if stats is None:
            return {"session_id": session_id, "message": "No stats recorded yet"}
        return stats

    # ── GET /api/sessions/{session_id}/steps ───────────────────────────

    @router.get("/{session_id}/steps")
    async def get_session_steps(
        session_id: str,
        service=Depends(get_service),
    ) -> list[dict]:
        """Get per-step execution log for a session."""
        rec = service.session_service.get_session(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        return service._stats_service.get_session_steps(session_id)

    # ── GET /api/sessions/{session_id}/diffs ──────────────────────────

    @router.get("/{session_id}/diffs")
    async def get_session_diffs(
        session_id: str,
        status: str | None = None,
        service=Depends(get_service),
    ) -> list[dict]:
        """Get file diffs for a session, optionally filtered by status."""
        rec = service.session_service.get_session(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        return service._stats_service.get_session_diffs(session_id, status=status)

    # ── GET /api/sessions/{session_id}/tree ─────────────────────────────
    # Returns the full parent-child session tree for subagent navigation.

    @router.get("/{session_id}/tree")
    async def get_session_tree(
        session_id: str,
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """Return the hierarchical session tree starting from this session.

        Each node contains session summary + recursive children list,
        capped at 5 levels deep (CC-aligned).
        """
        tree = service.session_service.get_session_tree(session_id)
        if tree is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        return tree

    # ── GET /api/sessions/{session_id}/worktrees ────────────────────────
    # List unresolved worktrees from child sessions that need attention.

    @router.get("/{session_id}/worktrees")
    async def get_pending_worktrees(
        session_id: str,
        service=Depends(get_service),
    ) -> list[dict[str, Any]]:
        """Return child worktrees awaiting apply/discard for a session."""
        pending = []
        for child in service._store.list_child_sessions(session_id):
            result = getattr(child, "agent_result", None)
            if result is None:
                continue
            from agent.session.models import WorktreeDisposition
            if (getattr(result, "worktree_disposition", None) is WorktreeDisposition.PRESERVED
                    and getattr(result, "worktree", None) is not None):
                wt = result.worktree
                pending.append({
                    "child_session_id": child.id,
                    "agent_name": child.agent_name,
                    "path": getattr(wt, "path", ""),
                    "revision": getattr(wt, "revision", ""),
                    "summary": getattr(result, "summary", "")[:200],
                })
        return pending

    return router
