"""
R3.4: WS Event Mapper — DomainEvent → WsEvent DTO.

Separates durable domain facts from ephemeral WebSocket notifications.
The mapper is a pure function: DomainEvent in → WsEvent out (or None).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def map_domain_to_ws(record) -> dict | None:
    """Map an outbox record payload to a WS event dict.

    Returns None for events that should not be broadcast to WebSocket
    subscribers (e.g., internal lifecycle events with no UI representation).
    """
    event_type = record.event_type
    payload = record.payload

    if event_type == "session.started":
        return {
            "type": "status", "status": "started",
            "session_id": record.session_id,
            "timestamp": record.occurred_at,
        }
    if event_type == "session.completed":
        return {
            "type": "status", "status": "completed",
            "session_id": record.session_id,
            "steps_taken": payload.get("steps_taken", 0),
            "timestamp": record.occurred_at,
        }
    if event_type == "session.cancelled":
        return {
            "type": "status", "status": "cancelled",
            "session_id": record.session_id,
            "reason": payload.get("reason", ""),
            "timestamp": record.occurred_at,
        }
    if event_type == "run.started":
        return {
            "type": "run_started", "run_id": record.aggregate_id,
            "session_id": record.session_id,
            "timestamp": record.occurred_at,
        }
    if event_type in ("run.completed", "run.cancelled", "run.failed"):
        return {
            "type": "run_terminal", "run_id": record.aggregate_id,
            "status": event_type.split(".")[1],
            "session_id": record.session_id,
            "steps_taken": payload.get("steps_taken", 0),
            "total_tokens": payload.get("tokens_used", 0),
            "error": payload.get("error", ""),
            "timestamp": record.occurred_at,
        }
    if event_type == "tool.executed":
        return {
            "type": "observation", "tool_name": payload.get("tool_name", ""),
            "invocation_id": payload.get("invocation_id", ""),
            "success": payload.get("success", True),
            "session_id": record.session_id,
            "timestamp": record.occurred_at,
        }

    # Unknown event type — no WS mapping
    logger.debug("No WS mapping for event type: %s", event_type)
    return None
