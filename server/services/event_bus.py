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
import re
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
    """Translate ``agent.task.Event`` → list of typed WS messages.

    One Event can produce multiple messages (e.g. ACTION → thought + tool_call).
    Uses server.events dataclasses as the single source of truth for shapes.
    """
    from agent.task import EventType
    from server.events import (
        WsStatus, WsThought, WsToolCall, WsObservation, WsReflection,
        WsSubagentStart, WsSubagentStop, WsPlanReady,
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
        # When a plan contract was produced (ExitPlanMode), emit plan_ready
        # so it can be recovered from /trace/events after page refresh.
        _contract = payload.get("contract")
        if _contract:
            return [WsPlanReady(
                plan_text=payload.get("summary", ""),
                contract=_contract,
                result={
                    "summary": payload.get("summary", ""),
                    "steps_taken": payload.get("steps", 0),
                },
                timestamp=ts,
            ).to_dict()]
        # Non-plan completion: the model's last assistant message IS the
        # completion notification — no redundant WsStatus needed.
        return []

    if ev_type == "task_failed":
        return [WsStatus(status="failed",
            error=payload.get("error", str(payload.get("reason", "unknown"))),
            timestamp=ts).to_dict()]

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

        atype = action.get("action_type", "")
        msg_text = action.get("message", "")
        if atype in ("finish", "give_up") and msg_text:
            msgs.append(WsStatus(status=atype, message=msg_text, timestamp=ts).to_dict())

        return msgs

    if ev_type == "observation":
        obs = payload.get("observation", {}) or {}
        return [WsObservation(
            tool_name=obs.get("tool_name", ""), output=obs.get("output", ""),
            error=obs.get("error"), status=obs.get("status", ""),
            step=payload.get("step", 0), id=payload.get("tool_call_id"),
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

    # Fallback: send raw event as-is
    return [{"type": ev_type, "payload": payload, "timestamp": ts}]


_DIFF_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "file_edit", "file_write"})

class EventBus:
    """Manages per-session event queues and WebSocket subscribers."""

    def __init__(self, repo_path: str = "") -> None:
        self._sessions: dict[str, SessionSubscriber] = {}
        self._lock = asyncio.Lock()
        self._repo_path = repo_path
        self.recorder: Any = None  # StatsRecorder instance, set by agent_service

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

    # ── Diff computation ────────────────────────────────────────────────────

    _FILE_PATH_RE = re.compile(r"(?:Edited |Created new file: |Applied edit to )(\S+)")

    def _compute_diff(self, tool_name: str, output: str) -> str | None:
        """Run git diff for a file modified by Edit/Write tool.

        Returns unified diff string, or None if diff can't be computed.
        """
        if tool_name not in ("Edit", "Write", "file_edit", "file_write"):
            return None
        if not self._repo_path:
            return None

        m = self._FILE_PATH_RE.search(output)
        if not m:
            return None
        filepath = m.group(1)

        try:
            import subprocess
            result = subprocess.run(
                ["git", "diff", "--", filepath],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=self._repo_path, timeout=5,
            )
            diff = result.stdout.strip()
            return diff if diff else None
        except Exception:
            return None

    def _git_diff_for_file(self, filepath: str) -> str | None:
        """Compute git diff for a known file path (no regex needed).

        Normalizes absolute paths to repo-relative for git diff.
        """
        if not self._repo_path or not filepath:
            return None
        # Normalize to repo-relative path
        import os as _os
        _repo = _os.path.abspath(self._repo_path)
        _fp = _os.path.abspath(filepath)
        if _fp.startswith(_repo + _os.sep):
            _fp = _fp[len(_repo) + 1:]
        try:
            import subprocess
            result = subprocess.run(
                ["git", "diff", "--", _fp],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=self._repo_path, timeout=5,
            )
            diff = result.stdout.strip()
            return diff if diff else None
        except Exception:
            return None

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
                        # Compute git diff for file-modifying observations.
                        # Uses Observation.modified_files from tool metadata,
                        # falls back to regex on output text.
                        if msg.get("type") == "observation" and not msg.get("error"):
                            _tool = msg.get("tool_name", "")
                            if _tool in _DIFF_TOOLS:
                                # Priority 1: explicit modified_files list from event payload
                                _event_payload = getattr(event, "payload", {}) or {}
                                _modified = _event_payload.get("observation", {}).get("modified_files", [])
                                if _modified:
                                    for _fp in _modified:
                                        diff = self._git_diff_for_file(_fp)
                                        if diff:
                                            msg["diff"] = diff
                                            break
                                # Priority 2: regex fallback
                                if not msg.get("diff"):
                                    diff = self._compute_diff(_tool, msg.get("output", ""))
                                    if diff:
                                        msg["diff"] = diff
                        logger.info("EVENT → %s | type=%s step=%s",
                                     target_session_id[:8], msg.get("type"), msg.get("step", ""))
                        sub.publish(msg)
                else:
                    logger.debug("EVENT dropped (no subscriber): session=%s", target_session_id[:8])
            else:
                # Legacy fallback: broadcast to all sessions (backward compat)
                logger.warning("EVENT broadcast (no session_id): type=%s",
                               getattr(event, "event_type", "?"))
                for sub in list(self._sessions.values()):
                    if sub.has_subscribers:
                        for msg in msgs:
                            sub.publish(msg)
            # Stats recording moved to first-party instrumentation in agent/core.py.
            # The recorder field is kept for backward compat but no longer called here.
        except Exception:
            logger.exception("EventBus.publish failed")

    def publish_raw(self, session_id: str, msg: dict[str, Any]) -> None:
        """Push a pre-formatted WS message to one session's subscribers.

        Prefer ``publish_typed()`` for new code — it enforces the
        event schema via server.events dataclasses.
        """
        try:
            sub = self._sessions.get(session_id)
            if sub is not None and sub.has_subscribers:
                sub.publish(msg)
        except Exception:
            logger.exception("EventBus.publish_raw failed")

    def publish_typed(self, session_id: str, event: Any) -> None:
        """Push a typed WS event (from server.events) to one session.

        The event must be a dataclass with a ``to_dict()`` method.
        This is the preferred API for new code — it ensures the event
        schema matches the frontend's expected shape.
        """
        try:
            sub = self._sessions.get(session_id)
            if sub is not None and sub.has_subscribers:
                sub.publish(event.to_dict())
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
