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

    @router.get("", response_model=list[SessionSummary])
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
            records = service.session_service.list_sessions(limit=limit, offset=offset)
            return [_summary_from_record(r) for r in records]
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
        rec = service.session_service.get_session(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
        return _detail_from_record(rec)

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

    # ── POST /api/sessions/{session_id}/chat ─────────────────────────────
    #
    # ═══════════════════════════════════════════════════════════════════════
    #  CORE ENDPOINT — Main ReAct execution entry point for the Web GUI
    # ═══════════════════════════════════════════════════════════════════════

    @router.post("/{session_id}/chat", response_model=ChatResponse)
    async def chat(
        session_id: str,
        body: ChatRequest,
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """
        Execute one chat round through the ReAct agent loop.

        This is the **core endpoint** of the Web MVP.  It runs the full
        ReAct loop — the agent thinks, decides on tool calls, executes
        them, observes results, and continues until the task is complete
        or a terminal condition is met.

        The execution is **synchronous from the client's perspective**: the
        endpoint blocks until the agent loop finishes, then returns the
        complete result.  For real-time event streaming during execution,
        connect to ``WS /api/ws/sessions/{session_id}``.

        **How it works:**
        1. The session is loaded from SessionStore (status → RUNNING).
        2. An AgentFactory assembles the agent with the correct tools, config,
           and prompt contracts based on the agent definition.
        3. The agent runs its ReAct loop step by step:
           - Build messages (system prompt + history + runtime context)
           - Call LLM → get Action (thought + tool calls OR finish)
           - Execute tools → get Observations
           - Record events to EventLog (also pushed to WebSocket subscribers)
           - Repeat until FINISH, GIVE_UP, MAX_STEPS, or error
        4. Session status is finalised (COMPLETED / FAILED / CANCELLED).
        5. New messages are persisted to SessionStore.

        **Real-time monitoring:**
        While this endpoint runs, connect a WebSocket to
        ``/api/ws/sessions/{session_id}`` to receive live events as each
        ReAct step completes.

        **Request Body:**
        - ``prompt`` (string, required): User's task description.
        - ``agent_name`` (string|null, default null): Override agent definition.
        - ``intent`` (string|null, default null): ``'edit'`` | ``'analysis'``.

        **Response (200):**
        - ``session_id`` (string): Executed session ID.
        - ``status`` (string): One of ``'success'`` | ``'failed'`` |
          ``'max_steps'`` | ``'gave_up'`` | ``'blocked'`` | ``'cancelled'``.
        - ``summary`` (string): Agent's final summary.
        - ``steps_taken`` (int): ReAct steps executed.
        - ``total_tokens`` (int): Tokens consumed.
        - ``error`` (string|null): Error message.
        - ``termination_reason`` (string|null): Why execution stopped.

        **Errors:**
        - 404: Session not found.
        - 422: Validation error (empty prompt).
        - 500: Internal execution error.
        """
        # Validate session exists
        rec = service.session_service.get_session(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

        effective_agent = body.agent_name or rec.agent_name

        try:
            # Subscribe event bus for this session
            if hasattr(service, "_event_bus") and service._event_bus is not None:
                sub = await service._event_bus.create_session(session_id)

            result = await service.chat(
                session_id=session_id,
                prompt=body.prompt,
                agent_name=effective_agent,
                intent=body.intent,
            )

            # Signal event bus that execution is complete
            if hasattr(service, "_event_bus") and service._event_bus is not None:
                sub = service._event_bus.get_subscriber(session_id)
                if sub is not None:
                    sub.signal_complete()

            return {
                "session_id": session_id,
                "status": result.status.value if hasattr(result.status, "value") else result.status,
                "summary": result.summary,
                "steps_taken": result.steps_taken,
                "total_tokens": result.total_tokens,
                "error": result.error,
                "termination_reason": (
                    result.termination_reason.value
                    if hasattr(result.termination_reason, "value")
                    else result.termination_reason
                ),
            }
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Chat execution failed for session %s", session_id)
            return JSONResponse(
                status_code=500,
                content={
                    "session_id": session_id,
                    "status": "failed",
                    "summary": "",
                    "steps_taken": 0,
                    "total_tokens": 0,
                    "error": str(exc),
                    "termination_reason": "internal_error",
                },
            )

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

    return router
