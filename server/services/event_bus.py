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
import logging
import threading
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

class SessionSubscriber:
    """Tracks one session's queue + set of WebSocket subscribers."""

    def __init__(
        self,
        session_id: str,
        loop: asyncio.AbstractEventLoop,
        *,
        queue_max_size: int = 0,
    ) -> None:
        self.session_id = session_id
        self.loop = loop
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=max(0, queue_max_size)
        )
        self.websockets: set[WebSocket] = set()
        self._drain_task: asyncio.Task[None] | None = None
        # Phase 3: delta merge state — keyed by block_id
        self.dropped_deltas: int = 0

    def subscribe(self, ws: WebSocket) -> None:
        self.websockets.add(ws)

    def unsubscribe(self, ws: WebSocket) -> None:
        self.websockets.discard(ws)

    @property
    def has_subscribers(self) -> bool:
        return bool(self.websockets)

    def publish(self, event: dict[str, Any]) -> None:
        """Apply bounded, lossless backpressure across the thread boundary."""
        event_copy = dict(event)
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is self.loop:
            self.loop.create_task(self.queue.put(event_copy))
            return
        future = asyncio.run_coroutine_threadsafe(
            self.queue.put(event_copy),
            self.loop,
        )
        # Runtime producers slow down when the websocket cannot keep up. This
        # bounds memory without dropping assistant answer content.
        future.result()

    def signal_complete(self) -> None:
        """Signal the drain task that no more events will arrive."""
        self.loop.call_soon_threadsafe(
            lambda: self.loop.create_task(self.queue.put(None))
        )

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
                        await asyncio.wait_for(
                            ws.send_json(event),
                            timeout=5.0,
                        )
                    except (ConnectionResetError, ConnectionAbortedError, OSError):
                        disconnected.append(ws)
                    except (TypeError, ValueError) as exc:
                        logger.error("Failed to serialize event: %s — event keys: %s", exc, list(event.keys())[:10])
                        # Serialization error — remove this ws to prevent retrying
                        # the same bad event on it.  The client should reconnect.
                        disconnected.append(ws)
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
            self._drain_task = self.loop.create_task(self._drain())

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
    """Translate ``agent.task.Event`` → list of typed WS messages.

    One Event can produce multiple messages (e.g. ACTION → thought + tool_call).
    Uses server.events dataclasses as the single source of truth for shapes.
    """
    from server.events import (
        WsStatus, WsThought, WsToolCall, WsObservation, WsReflection,
        WsSubagentStart, WsSubagentStop, WsPlanReady, WsDelegationEvent,
    )

    ev_type = getattr(event, "event_type", "")
    if hasattr(ev_type, "value"):
        ev_type = ev_type.value
    payload = getattr(event, "payload", {}) or {}
    ts = getattr(event, "timestamp", "")
    child_id = getattr(event, "child_session_id", "")

    if ev_type == "task_start":
        return [WsStatus(status="running", timestamp=ts).to_dict()]

    if ev_type == "task_complete":
        _result: dict = {
            "summary": payload.get("summary", ""),
            "steps_taken": payload.get("steps", 0),
        }
        _cache = payload.get("cache")
        if _cache:
            _result["cache"] = _cache
        msgs: list[dict] = [WsStatus(status="completed", result=_result, timestamp=ts).to_dict()]
        _contract = payload.get("contract")
        if _contract:
            msgs.append(WsPlanReady(
                plan_text=payload.get("summary", ""),
                contract=_contract,
                result={"summary": payload.get("summary", ""), "steps_taken": payload.get("steps", 0)},
                timestamp=ts,
            ).to_dict())
        return msgs

    if ev_type == "task_failed":
        error = str(payload.get("error") or payload.get("reason") or "unknown")
        explicit_status = str(payload.get("status", "")).strip().lower()
        is_cancelled = (
            explicit_status in {"cancelled", "canceled"}
            or payload.get("cancelled") is True
        )
        return [WsStatus(
            status="cancelled" if is_cancelled else "failed",
            error=error,
            timestamp=ts,
        ).to_dict()]

    if ev_type == "action":
        action = payload.get("action", {}) or {}
        step = payload.get("step", 0)
        msgs: list[dict] = []

        thought = action.get("thought", "")
        if thought and thought.strip():
            msgs.append(WsThought(content=thought, step=step,
                child_session_id=child_id, timestamp=ts).to_dict())

        for tc in (action.get("tool_calls") or []):
            msgs.append(WsToolCall(
                name=tc.get("name", ""), params=tc.get("params", {}),
                step=step, id=tc.get("id", ""),
                child_session_id=child_id, timestamp=ts).to_dict())

        # P0: finish / give_up status events are suppressed.
        # The final turn lifecycle is now handled by run_terminal
        # (emitted AFTER DB commit in run_session's finally block).
        # The assistant answer text is streamed via assistant_text_delta
        # during _stream_and_dispatch.
        return msgs

    if ev_type == "observation":
        obs = payload.get("observation", {}) or {}
        _obs_meta = obs.get("metadata", {}) or {}
        _tc_id = payload.get("tool_call_id") or obs.get("tool_call_id") or ""
        return [WsObservation(
            tool_name=obs.get("tool_name", ""), output=obs.get("output", ""),
            error=obs.get("error"), status=obs.get("status", ""),
            step=payload.get("step", 0), id=_tc_id,
            tool_call_id=_tc_id,  # explicit tool_call_id for frontend matching
            diff=_obs_meta.get("diff", ""),
            child_session_id=child_id, timestamp=ts).to_dict()]

    if ev_type == "reflection":
        return [WsReflection(
            content=payload.get("reason", "") or str(payload.get("reflection", "")),
            timestamp=ts).to_dict()]

    if ev_type in ("subagent_start",):
        return [WsSubagentStart(
            child_session_id=payload.get("child_session_id", ""),
            agent_name=payload.get("agent_name", ""), timestamp=ts).to_dict()]

    if ev_type in ("subagent_stop", "subagent_complete"):
        return [WsSubagentStop(
            child_session_id=payload.get("child_session_id", ""),
            status=payload.get("status", "completed"), timestamp=ts).to_dict()]

    if str(ev_type).startswith("delegation_"):
        allowed = set(WsDelegationEvent.__dataclass_fields__) - {"type"}
        values = {key: value for key, value in payload.items() if key in allowed}
        values.setdefault("delegation_run_id", getattr(event, "task_id", ""))
        values.setdefault("timestamp", ts)
        values.setdefault("event_id", getattr(event, "event_id", ""))
        return [WsDelegationEvent(type=ev_type, **values).to_dict()]

    # Fallback: send raw event as-is
    return [{"type": ev_type, "payload": payload, "timestamp": ts}]


class EventBus:
    """Manages per-session event queues and WebSocket subscribers."""

    def __init__(
        self,
        repo_path: str = "",
        *,
        queue_max_size: int = 0,
    ) -> None:
        self._sessions: dict[str, SessionSubscriber] = {}
        self._lock = asyncio.Lock()
        self._publish_lock = threading.Lock()  # protects _sessions reads from sync thread
        self._repo_path = repo_path
        self._queue_max_size = max(0, int(queue_max_size))
        self.recorder: Any = None  # StatsRecorder instance, set by agent_service
        self.trace_store: Any = None  # StorageBackend, set by agent_service
        self.trace_cache: Any = None  # InMemoryTraceCache, set by agent_service

    # ── Session lifecycle ──────────────────────────────────────────────────

    async def create_session(self, session_id: str) -> SessionSubscriber:
        async with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                return existing
            loop = asyncio.get_running_loop()
            sub = SessionSubscriber(
                session_id,
                loop,
                queue_max_size=self._queue_max_size,
            )
            self._sessions[session_id] = sub
            return sub

    async def destroy_session(self, session_id: str) -> None:
        async with self._lock:
            sub = self._sessions.get(session_id)
            if sub is None or sub.has_subscribers:
                return  # re-subscribed between unsubscribe and destroy — keep alive
            self._sessions.pop(session_id, None)
        if sub is not None:
            sub.signal_complete()
            await sub.stop_drain()
        if self.trace_cache is not None:
            try:
                self.trace_cache.clear_session(session_id)
            except Exception:
                logger.debug("Trace cache cleanup failed", exc_info=True)

    def get_subscriber(self, session_id: str) -> SessionSubscriber | None:
        return self._sessions.get(session_id)

    # ── Publish (called from SessionRuntime thread) ────────────────────────

    def _persist_trace_event(self, session_id: str, msg: dict[str, Any], *, source: str = "event_bus") -> dict[str, Any]:
        stored = msg
        if self.trace_store is not None:
            try:
                stored = self.trace_store.insert_trace_event(session_id, msg, source=source)
            except Exception:
                logger.exception("Trace persistence failed — session=%s type=%s", session_id[:8], msg.get("type"))
                stored = msg
        if self.trace_cache is not None:
            try:
                self.trace_cache.append(session_id, stored)
            except Exception:
                logger.debug("Trace cache append failed", exc_info=True)
        return stored

    def _publish_msg(
        self,
        session_id: str,
        msg: dict[str, Any],
        *,
        source: str = "event_bus",
        run_context: Any = None,
    ) -> None:
        # ── Inject EventEnvelope: run_id / turn_id / turn_index ──
        # Passed explicitly per-call — NO shared mutable state on EventBus.
        if run_context is not None:
            # Typed event dataclasses deliberately serialize empty envelope
            # fields.  ``setdefault`` therefore leaves those empty strings in
            # place and the persisted event can no longer be assigned to a
            # turn during timeline replay.  Fill values that are absent *or*
            # empty while preserving an explicitly populated child context.
            if not msg.get("session_id"):
                msg["session_id"] = session_id
            if not msg.get("run_id"):
                msg["run_id"] = getattr(run_context, "run_id", "")
            if not msg.get("turn_id"):
                msg["turn_id"] = getattr(run_context, "turn_id", "")
            if not msg.get("turn_index"):
                msg["turn_index"] = getattr(run_context, "turn_index", 0)
        stored = self._persist_trace_event(session_id, msg, source=source)
        with self._publish_lock:
            sub = self._sessions.get(session_id)
        if sub is not None and sub.has_subscribers:
            sub.publish(stored)

    def publish(self, event: Any, *, run_context: Any = None) -> None:
        """Synchronous callback — called from SessionRuntime thread.

        Translates ``agent.task.Event`` objects into the standardized WS
        message format and pushes them to session subscribers.

        When *run_context* is provided, its run_id / turn_id / turn_index
        are injected into every translated message as envelope fields.
        """
        try:
            persisted = (getattr(event, "payload", {}) or {}).get("_persisted_event")
            target_session_id = getattr(event, "session_id", None)
            if isinstance(persisted, dict) and target_session_id:
                # The run state and terminal trace were committed atomically by
                # SessionStore. EventBus remains the sole live broadcast path,
                # but must not persist the terminal event a second time.
                if self.trace_cache is not None:
                    try:
                        self.trace_cache.append(target_session_id, persisted)
                    except Exception:
                        logger.debug("Trace cache append failed", exc_info=True)
                with self._publish_lock:
                    sub = self._sessions.get(target_session_id)
                if sub is not None and sub.has_subscribers:
                    sub.publish(persisted)
                return
            msgs = _translate_event(event)
            if target_session_id:
                for msg in msgs:
                    logger.info("EVENT → %s | type=%s step=%s",
                                 target_session_id[:8], msg.get("type"), msg.get("step", ""))
                    self._publish_msg(target_session_id, msg, run_context=run_context)
                if target_session_id not in self._sessions:
                    logger.debug("EVENT persisted without subscriber: session=%s", target_session_id[:8])
            else:
                logger.debug("EVENT dropped (no session_id): type=%s",
                               getattr(event, "event_type", "?"))
        except Exception:
            logger.exception("EventBus.publish failed")

    def publish_raw(
        self,
        session_id: str,
        msg: dict[str, Any],
        *,
        run_context: Any = None,
        skip_persist: bool = False,
    ) -> None:
        """Push a pre-formatted WS message to one session's subscribers.

        When *skip_persist* is True, the message is broadcast directly
        without inserting into session_trace_events.  Use this when the
        event has already been persisted (e.g. run_terminal from
        transactional_finalize_run).
        """
        try:
            if skip_persist:
                # ── Broadcast only — event already persisted ──
                with self._publish_lock:
                    sub = self._sessions.get(session_id)
                if sub is not None and sub.has_subscribers:
                    sub.publish(msg)
            else:
                self._publish_msg(session_id, msg, source="raw", run_context=run_context)
        except Exception:
            logger.exception("EventBus.publish_raw failed")

    def publish_typed(
        self, session_id: str, event: Any, *, run_context: Any = None,
    ) -> None:
        """Push a typed WS event (from server.events) to one session.

        The event must be a dataclass with a ``to_dict()`` method.
        This is the preferred API for new code — it ensures the event
        schema matches the frontend's expected shape.
        """
        try:
            self._publish_msg(session_id, event.to_dict(), source="typed", run_context=run_context)
        except Exception:
            logger.exception("EventBus.publish_typed failed")

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
