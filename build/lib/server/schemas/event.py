"""
Pydantic schemas for EventLog and WebSocket event streaming.

These models document the event payloads sent over WebSocket and returned
by the events REST API endpoint.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class EventResponse(BaseModel):
    """A single execution event from the EventLog.

    Events are the structured record of every step in the ReAct loop:
    actions (model decisions), observations (tool results), reflections,
    subagent lifecycle events, etc.
    """

    event_id: str = Field(
        description="8-character hex unique identifier for this event.",
    )
    event_type: str = Field(
        description="Event type categorising this entry. "
        "Common values: 'task_start', 'action', 'observation', "
        "'reflection', 'subagent_start', 'subagent_stop', "
        "'task_complete', 'task_failed'.",
    )
    task_id: str = Field(
        description="The task ID this event belongs to.",
    )
    timestamp: str = Field(
        description="ISO-8601 timestamp when the event occurred.",
    )
    payload: dict = Field(
        description="Event-type-specific payload. "
        "For 'action' events: ``{'step': int, 'action': {'action_type': str, "
        "'thought': str, 'tool_calls': [...]}}``. "
        "For 'observation' events: ``{'step': int, 'observation': "
        "{'tool_name': str, 'status': str, 'output': str}}``.",
    )


class WebSocketMessage(BaseModel):
    """Message envelope for WebSocket streaming.

    The server pushes one JSON message per event to subscribed WebSocket
    clients.  Each message wraps an execution event with a top-level type
    discriminator.

    Example flow:
        ``{"type": "task_start", ...}``
        ``{"type": "action", "step": 1, ...}``
        ``{"type": "observation", "step": 1, ...}``
        ``{"type": "action", "step": 2, ...}``
        ``{"type": "observation", "step": 2, ...}``
        ``{"type": "task_complete", "result": {...}}``
    """

    type: str = Field(
        description="Message type discriminator. "
        "One of: 'task_start', 'action', 'observation', 'reflection', "
        "'subagent_start', 'subagent_stop', 'task_complete', 'task_failed', "
        "'complete' (sentinel — no more events).",
    )
    payload: dict = Field(
        description="Event payload. Shape depends on ``type``.",
    )
    timestamp: str = Field(
        description="ISO-8601 timestamp.",
    )
    event_id: str | None = Field(
        default=None,
        description="Unique event identifier (absent on sentinel messages).",
    )


class EventsListResponse(BaseModel):
    """Response wrapper for ``GET /api/sessions/{id}/events``."""

    events: list[EventResponse] = Field(
        description="Array of events matching the query parameters.",
    )
    total: int = Field(
        description="Total number of events available (before pagination).",
    )
    has_more: bool = Field(
        description="True if more events exist beyond the returned page.",
    )
