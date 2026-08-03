"""
G38: DEPRECATED — replaced by server.ws.native_event_mapper.NativeEventMapper.

Old mapper uses record.payload dict access with .get() calls.
New code MUST use NativeEventMapper which reads from Typed EventEnvelope.
Kept for backward compat only — will be removed in G42.
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
    # G38: Normalize versioned event types (e.g. "run.submitted.v1" → "run.submitted")
    # to maintain backward compat with old unversioned event type checks.
    if event_type.endswith((".v1", ".v2", ".v3")):
        event_type = event_type.rsplit(".", 1)[0]
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
    # run.submitted is deliberately not mapped — it's an internal event
    # with no UI representation.  The TraceProjection fallback in
    # server/projections/trace_projection.py still records it in
    # session_trace_events so the trace assertion sees "run.submitted".
    if event_type == "run.started":
        return {
            "type": "run_started", "run_id": record.aggregate_id,
            "session_id": record.session_id,
            "turn_id": payload.get("turn_id", ""),
            "turn_index": payload.get("turn_index", 0),
            "timestamp": record.occurred_at,
        }
    if event_type.startswith("run.") and event_type not in (
        "run.submitted", "run.started",
    ):
        return {
            "type": "run_terminal", "run_id": record.aggregate_id,
            "status": payload.get("status", event_type.split(".", 1)[1]),
            "session_id": record.session_id,
            "turn_id": payload.get("turn_id", ""),
            "turn_index": payload.get("turn_index", 0),
            "summary": payload.get("summary", ""),
            "steps_taken": payload.get("steps_taken", 0),
            "total_tokens": payload.get("total_tokens", payload.get("tokens_used", 0)),
            "error": payload.get("error", ""),
            "termination_reason": payload.get("termination_reason", "none"),
            "verification_status": payload.get("verification_status", "not_applicable"),
            "verification_reason": payload.get("verification_reason", "none"),
            "verification": payload.get("verification", {}),
            "workspace_delta": payload.get("workspace_delta", {}),
            "evidence_summary": payload.get("evidence_summary", {}),
            "timestamp": record.occurred_at,
            "event_id": record.event_id,
        }
    if event_type == "tool.executed":
        return {
            "type": "observation", "tool_name": payload.get("tool_name", ""),
            "invocation_id": payload.get("invocation_id", ""),
            "success": payload.get("success", True),
            "session_id": record.session_id,
            "timestamp": record.occurred_at,
        }
    if event_type == "delegation.completed":
        return {
            "type": "delegation_completed",
            "event_id": record.event_id,
            "session_id": record.session_id,
            "run_id": payload.get("run_id", ""),
            "delegation_run_id": record.aggregate_id,
            "status": payload.get("status", ""),
            "phase": payload.get("phase", ""),
            "report_count": payload.get("report_count", 0),
            "version": payload.get("version", record.aggregate_version),
            "timestamp": record.occurred_at,
        }

    # Unknown event type — no WS mapping
    logger.debug("No WS mapping for event type: %s", event_type)
    return None
