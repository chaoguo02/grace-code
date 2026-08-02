"""In-process hot cache for typed session trace events.

This is intentionally small and optional.  Redis is the right long-term
implementation for multi-process pub/sub and shared reconnect buffers; this
cache gives the current single-process server the same seam without adding a
runtime dependency.
"""

from __future__ import annotations

from collections import OrderedDict
from threading import RLock
from typing import Any


class InMemoryTraceCache:
    """Bounded per-session cache of backend-owned typed trace events."""

    def __init__(self, *, max_sessions: int = 128, max_events_per_session: int = 500) -> None:
        self._max_sessions = max(1, max_sessions)
        self._max_events_per_session = max(1, max_events_per_session)
        self._events_by_session: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        self._lock = RLock()

    def append(self, session_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            events = self._events_by_session.setdefault(session_id, [])
            events.append(dict(event))
            if len(events) > self._max_events_per_session:
                del events[:len(events) - self._max_events_per_session]
            self._events_by_session.move_to_end(session_id)
            while len(self._events_by_session) > self._max_sessions:
                self._events_by_session.popitem(last=False)

    def list_after(self, session_id: str, *, after_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            events = self._events_by_session.get(session_id)
            if not events:
                return []
            self._events_by_session.move_to_end(session_id)
            result = [
                dict(event)
                for event in events
                if int(event.get("seq") or 0) > after_seq
            ]
            return result[:limit]

    def clear_session(self, session_id: str) -> None:
        with self._lock:
            self._events_by_session.pop(session_id, None)
