"""
WebSocket router — real-time event streaming during agent execution.

Mounted under ``WS /api/ws/sessions/{session_id}``.

The WebSocket streams execution events as they happen, enabling the
frontend TimelinePanel to show the ReAct loop step-by-step in real time.

Protocol:
    1. Client connects to ``/api/ws/sessions/{session_id}``.
    2. Server subscribes the client to the session's EventBus.
    3. Server sends one JSON message per event:
       - ``{"type": "task_start",    "payload": {...}, "timestamp": "..."}``
       - ``{"type": "action",        "step": 1, "action": {...}, "timestamp": "..."}``
       - ``{"type": "observation",   "step": 1, "observation": {...}, "timestamp": "..."}``
       - ``{"type": "reflection",    "reason": "...", "timestamp": "..."}``
       - ``{"type": "subagent_start", "child_session_id": "...", ...}``
       - ``{"type": "subagent_stop",  "child_session_id": "...", ...}``
       - ``{"type": "task_complete",  "result": {...}, "timestamp": "..."}``
       - ``{"type": "task_failed",    "error": "...", "timestamp": "..."}``
    4. When execution finishes, server sends ``{"type": "complete"}``
       and closes the connection.

    The client can send a JSON ``{"action": "cancel"}`` message at any
    time to request session cancellation.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


def create_websocket_router(get_service: Any) -> APIRouter:
    """Create the WebSocket router with dependency injection.

    Args:
        get_service: FastAPI dependency callable returning AgentService.

    Returns:
        APIRouter configured with WebSocket endpoints.
    """
    router = APIRouter(tags=["websocket"])

    # ── WS /api/ws/sessions/{session_id} ─────────────────────────────────

    @router.websocket("/api/ws/sessions/{session_id}")
    async def session_events_ws(
        session_id: str,
        websocket: WebSocket,
        service=Depends(get_service),
    ) -> None:
        """
        Stream session execution events in real time.

        **Path Parameters:**
        - ``session_id`` (string): 12-char hex session ID.

        **Protocol (server → client):**
        The server pushes one JSON message per event.  Each message has a
        top-level ``type`` discriminator:

        - ``task_start``: Task has begun. Payload contains the task config.
        - ``action``: Agent made a decision. Payload has ``step`` and
          ``action`` with ``thought`` and ``tool_calls``.
        - ``observation``: Tool execution result. Payload has ``step``,
          ``tool_name``, ``status``, ``output``.
        - ``reflection``: Agent is reflecting. Payload has ``reason``.
        - ``subagent_start``: A subagent was spawned.
        - ``subagent_stop``: A subagent completed.
        - ``task_complete`` / ``task_failed``: Execution finished.
        - ``complete``: Sentinel — no more events. Server will close.

        **Protocol (client → server):**
        - ``{"action": "cancel"}``: Cancel the running session.
        - ``{"action": "ping"}``: Keep-alive. Server responds with
          ``{"type": "pong"}``.

        **Errors:**
        - 404 close code: Session not found.
        - 1000 close code: Normal completion.
        - 1001 close code: Session cancelled.
        """
        # Validate session exists
        rec = service.session_service.get_session(session_id)
        if rec is None:
            await websocket.close(code=4004, reason=f"Session not found: {session_id}")
            return

        await websocket.accept()
        logger.info("WebSocket connected for session %s", session_id)

        # Subscribe to event bus
        if hasattr(service, "_event_bus") and service._event_bus is not None:
            await service._event_bus.subscribe(session_id, websocket)
        else:
            # No event bus — just send a placeholder and close
            await websocket.send_json({"type": "complete", "payload": {}, "timestamp": ""})
            await websocket.close(code=1000)
            return

        try:
            # Listen for client messages (cancel, ping)
            while True:
                try:
                    data = await websocket.receive_text()
                    msg = json.loads(data)
                    action = msg.get("action", "")
                    if action == "cancel":
                        service.cancel_session(session_id, detail="User cancelled via WebSocket")
                        logger.info("Cancel requested via WS for session %s", session_id)
                    elif action == "ping":
                        await websocket.send_json({"type": "pong"})
                except json.JSONDecodeError:
                    pass  # Ignore malformed messages
        except WebSocketDisconnect:
            logger.info("WebSocket disconnected for session %s", session_id)
        except Exception as exc:
            logger.exception("WebSocket error for session %s: %s", session_id, exc)
        finally:
            # Unsubscribe
            if hasattr(service, "_event_bus") and service._event_bus is not None:
                await service._event_bus.unsubscribe(session_id, websocket)

    return router
