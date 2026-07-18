"""
Event bus — bridges synchronous SessionRuntime event_callback to async WebSocket.

Architecture:
  SessionRuntime thread  ──publish()──>  asyncio.Queue  ──drain task──>  WebSocket

Each session gets its own queue. The publish() method is called from the
SessionRuntime thread (via event_callback). It pushes events into the queue
using loop.call_soon_threadsafe(). A background asyncio task drains the queue
and broadcasts to all subscribed WebSocket clients.

Usage:
    bus = EventBus()
    bus.subscribe(session_id, websocket)
    bus.start_drain(session_id)

    # In SessionRuntime init:
    runtime = SessionRuntime(..., event_callback=bus.publish)

    # When SessionRuntime finishes:
    bus.unsubscribe_all(session_id)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class SessionSubscriber:
    """Tracks one session's queue + set of WebSocket subscribers."""

    def __init__(self, session_id: str, loop: asyncio.AbstractEventLoop) -> None:
        self.session_id = session_id
        self.loop = loop
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.websockets: set[WebSocket] = set()
        self._drain_task: asyncio.Task[None] | None = None

    def subscribe(self, ws: WebSocket) -> None:
        self.websockets.add(ws)

    def unsubscribe(self, ws: WebSocket) -> None:
        self.websockets.discard(ws)

    @property
    def has_subscribers(self) -> bool:
        return bool(self.websockets)

    def publish(self, event: dict[str, Any]) -> None:
        """Called from SessionRuntime thread. Thread-safe via call_soon_threadsafe."""
        self.loop.call_soon_threadsafe(self.queue.put_nowait, event)

    def signal_complete(self) -> None:
        """Signal the drain task that no more events will arrive."""
        self.loop.call_soon_threadsafe(self.queue.put_nowait, None)

    async def _drain(self) -> None:
        """Background task: drain queue and broadcast to all subscribers."""
        try:
            while True:
                event = await self.queue.get()
                if event is None:  # sentinel → shutdown
                    break
                disconnected: list[WebSocket] = []
                for ws in self.websockets:
                    try:
                        await ws.send_json(event)
                    except Exception:
                        disconnected.append(ws)
                for ws in disconnected:
                    self.websockets.discard(ws)
        except asyncio.CancelledError:
            pass
        finally:
            # Flush remaining events on cancellation
            while not self.queue.empty():
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

    def start_drain(self) -> None:
        if self._drain_task is None:
            self._drain_task = asyncio.ensure_future(self._drain(), loop=self.loop)

    async def stop_drain(self) -> None:
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None


# ─── Event translation ───────────────────────────────────────────────────────

def _translate_event(event: Any) -> list[dict[str, Any]]:
    """Translate ``agent.task.Event`` → list of standardized WS messages.

    One Event can produce multiple messages (e.g. ACTION → thought + tool_call).
    """
    from agent.task import EventType

    ev_type = getattr(event, "event_type", "")
    if hasattr(ev_type, "value"):
        ev_type = ev_type.value
    payload = getattr(event, "payload", {}) or {}
    ts = getattr(event, "timestamp", "")

    if ev_type == "task_start":
        return [{"type": "status", "status": "running", "timestamp": ts}]

    if ev_type == "task_complete":
        return [{
            "type": "status", "status": "completed",
            "result": {
                "summary": payload.get("summary", ""),
                "steps_taken": payload.get("steps", 0),
            },
            "timestamp": ts,
        }]

    if ev_type == "task_failed":
        return [{
            "type": "status", "status": "failed",
            "error": payload.get("error", str(payload.get("reason", "unknown"))),
            "timestamp": ts,
        }]

    if ev_type == "action":
        action = payload.get("action", {}) or {}
        step = payload.get("step", 0)
        msgs: list[dict[str, Any]] = []

        thought = action.get("thought", "")
        if thought and thought.strip():
            msgs.append({"type": "thought", "content": thought, "step": step, "timestamp": ts})

        tool_calls = action.get("tool_calls") or []
        for tc in tool_calls:
            msgs.append({
                "type": "tool_call",
                "step": step,
                "name": tc.get("name", ""),
                "params": tc.get("params", {}),
                "id": tc.get("id", ""),
                "timestamp": ts,
            })

        # finish / give_up have a message
        atype = action.get("action_type", "")
        msg_text = action.get("message", "")
        if atype in ("finish", "give_up") and msg_text:
            msgs.append({"type": "status", "status": atype, "message": msg_text, "timestamp": ts})

        return msgs

    if ev_type == "observation":
        obs = payload.get("observation", {}) or {}
        return [{
            "type": "observation",
            "step": payload.get("step", 0),
            "tool_name": obs.get("tool_name", ""),
            "status": obs.get("status", ""),
            "output": obs.get("output", ""),
            "error": obs.get("error"),
            "id": payload.get("tool_call_id"),
            "timestamp": ts,
        }]

    if ev_type == "reflection":
        return [{
            "type": "reflection",
            "content": payload.get("reason", "") or str(payload.get("reflection", "")),
            "timestamp": ts,
        }]

    if ev_type in ("subagent_start",):
        return [{
            "type": "subagent_start",
            "child_session_id": payload.get("child_session_id", ""),
            "agent_name": payload.get("agent_name", ""),
            "timestamp": ts,
        }]

    if ev_type in ("subagent_stop", "subagent_complete"):
        return [{
            "type": "subagent_stop",
            "child_session_id": payload.get("child_session_id", ""),
            "status": payload.get("status", "completed"),
            "timestamp": ts,
        }]

    # Fallback: send raw event as-is
    return [{
        "type": ev_type,
        "payload": payload,
        "timestamp": ts,
    }]


class EventBus:
    """Manages per-session event queues and WebSocket subscribers."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionSubscriber] = {}
        self._lock = asyncio.Lock()

    # ── Session lifecycle ──────────────────────────────────────────────────

    async def create_session(self, session_id: str) -> SessionSubscriber:
        async with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                return existing
            loop = asyncio.get_running_loop()
            sub = SessionSubscriber(session_id, loop)
            self._sessions[session_id] = sub
            return sub

    async def destroy_session(self, session_id: str) -> None:
        async with self._lock:
            sub = self._sessions.pop(session_id, None)
        if sub is not None:
            sub.signal_complete()
            await sub.stop_drain()

    def get_subscriber(self, session_id: str) -> SessionSubscriber | None:
        return self._sessions.get(session_id)

    # ── Publish (called from SessionRuntime thread) ────────────────────────

    def publish(self, event: Any) -> None:
        """Synchronous callback — called from SessionRuntime thread.

        Translates ``agent.task.Event`` objects into the standardized WS
        message format and pushes them to session subscribers.

        Routes events to the correct session when ``event.session_id`` is set.
        Falls back to broadcast (all sessions) only when no session_id is
        available (backward compatibility for code paths that haven't been
        updated yet).

        Standard WS message types:
            status          — session state change (running/completed/failed)
            thought         — model's thinking text
            tool_call       — tool invocation (name + params)
            observation     — tool result (output/error)
            reflection      — model reflection
            subagent_start  — child session spawned
            subagent_stop   — child session finished
        """
        try:
            msgs = _translate_event(event)
            target_session_id = getattr(event, "session_id", None)
            if target_session_id:
                # Route to the specific session that generated this event
                sub = self._sessions.get(target_session_id)
                if sub is not None and sub.has_subscribers:
                    for msg in msgs:
                        sub.publish(msg)
            else:
                # Legacy fallback: broadcast to all sessions (backward compat)
                for sub in list(self._sessions.values()):
                    if sub.has_subscribers:
                        for msg in msgs:
                            sub.publish(msg)
        except Exception:
            logger.exception("EventBus.publish failed")

    def publish_raw(self, session_id: str, msg: dict[str, Any]) -> None:
        """Push a pre-formatted WS message to one session's subscribers.

        Used for sending status events from outside the SessionRuntime
        callback chain (e.g. ``status: completed`` after run finishes).
        """
        try:
            sub = self._sessions.get(session_id)
            if sub is not None and sub.has_subscribers:
                sub.publish(msg)
        except Exception:
            logger.exception("EventBus.publish_raw failed")

    # ── Subscriber management ──────────────────────────────────────────────

    async def subscribe(self, session_id: str, ws: WebSocket) -> None:
        sub = self.get_subscriber(session_id)
        if sub is None:
            sub = await self.create_session(session_id)
        sub.subscribe(ws)
        sub.start_drain()

    async def unsubscribe(self, session_id: str, ws: WebSocket) -> None:
        sub = self.get_subscriber(session_id)
        if sub is not None:
            sub.unsubscribe(ws)
            if not sub.has_subscribers:
                await self.destroy_session(session_id)
