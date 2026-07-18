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


class EventBus:
    """Manages per-session event queues and WebSocket subscribers."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionSubscriber] = {}
        self._lock = asyncio.Lock()

    # ── Session lifecycle ──────────────────────────────────────────────────

    async def create_session(self, session_id: str) -> SessionSubscriber:
        async with self._lock:
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

        Serializes the Event domain object to a dict and pushes it onto the
        session's asyncio.Queue via call_soon_threadsafe.

        This is wired as ``SessionRuntime(event_callback=event_bus.publish)``.
        """
        try:
            # event is an agent.task.Event domain object
            serialized = {
                "event_id": getattr(event, "event_id", ""),
                "event_type": getattr(event, "event_type", ""),
                "task_id": getattr(event, "task_id", ""),
                "timestamp": getattr(event, "timestamp", ""),
                "payload": getattr(event, "payload", {}),
            }
            # Normalise enum values to strings
            if hasattr(serialized["event_type"], "value"):
                serialized["event_type"] = serialized["event_type"].value

            # Find the subscriber for this event's task_id → session_id mapping.
            # We need to reverse-map task_id to session_id. The subscriber
            # lookup uses session_id, not task_id. This means the caller should
            # set the session_id on the event or we need a task_id→session_id map.
            #
            # For the initial implementation we broadcast to ALL sessions.
            # This is safe because typically only one session is active at a time.
            # A future improvement will maintain a task_id→session_id mapping.
            for sub in list(self._sessions.values()):
                if sub.has_subscribers:
                    sub.publish(serialized)
        except Exception:
            logger.exception("EventBus.publish failed")

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
