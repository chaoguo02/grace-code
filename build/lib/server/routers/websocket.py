"""
WebSocket router — real-time event streaming during agent execution.

Mounted under ``WS /api/ws/sessions/{session_id}``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


def create_websocket_router(service: Any) -> APIRouter:
    """Create the WebSocket router with direct service injection."""
    router = APIRouter(tags=["websocket"])

    @router.websocket("/api/ws/sessions/{session_id}")
    async def session_events_ws(
        session_id: str,
        websocket: WebSocket,
    ) -> None:
        # ── ALWAYS accept first, then validate ──
        # Closing before accept() causes the browser to see code=1006
        # (abnormal closure) instead of our explicit close code.
        await websocket.accept()
        logger.info("WS accepted — session=%s", session_id)

        # Check if session exists
        rec = service.session_service.get_session(session_id)
        if rec is None:
            logger.warning("WS session not found — %s", session_id)
            await websocket.close(code=4004, reason=f"Session not found: {session_id}")
            return

        # Subscribe to EventBus
        if not (hasattr(service, "_event_bus") and service._event_bus is not None):
            logger.warning("WS no EventBus — closing session=%s", session_id)
            await websocket.send_json({"type": "complete", "payload": {}, "timestamp": ""})
            await websocket.close(code=1000)
            return

        try:
            await service._event_bus.subscribe(session_id, websocket)
            logger.info("WS subscribed to EventBus — session=%s", session_id)
        except Exception as exc:
            logger.exception("WS subscribe failed — session=%s", session_id)
            await websocket.close(code=1011, reason=f"Internal error: {exc}")
            return

        try:
            while True:
                try:
                    data = await websocket.receive_text()
                    msg = json.loads(data)
                    action = msg.get("action", "")
                    if action == "cancel":
                        service.cancel_session(session_id, detail="User cancelled via WebSocket")
                        logger.info("Cancel via WS — session=%s", session_id)
                    elif action == "ping":
                        await websocket.send_json({"type": "pong"})
                except json.JSONDecodeError:
                    pass
        except WebSocketDisconnect:
            logger.info("WS disconnected — session=%s", session_id)
        except Exception as exc:
            logger.exception("WS error — session=%s: %s", session_id, exc)
        finally:
            if hasattr(service, "_event_bus") and service._event_bus is not None:
                try:
                    await service._event_bus.unsubscribe(session_id, websocket)
                except (ConnectionResetError, OSError):
                    pass  # client already disconnected — Windows proactor noise

    return router
