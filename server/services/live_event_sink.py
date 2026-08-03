"""
R3.4: Live Event Sink — bounded queue for ephemeral WebSocket events.

Ephemeral events (text deltas, progress, heartbeat) do NOT go through
the transactional outbox.  They go through this bounded sink instead.

Overflow policy: oldest events dropped when queue is full.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class LiveEvent:
    session_id: str
    payload: dict


class LiveEventSink:
    """Bounded in-memory queue for ephemeral WS events.

    Not durable.  On disconnect, events are lost.  Clients recover
    via Trace projection, not by replaying live events.
    """

    DEFAULT_MAX_SIZE = 256

    def __init__(self, max_size: int | None = None) -> None:
        self._max = max_size or self.DEFAULT_MAX_SIZE
        self._queues: dict[str, deque[LiveEvent]] = {}

    def publish(self, session_id: str, payload: dict) -> None:
        """Push an ephemeral event for *session_id*."""
        if session_id not in self._queues:
            self._queues[session_id] = deque(maxlen=self._max)
        q = self._queues[session_id]
        if len(q) >= self._max:
            q.popleft()  # drop oldest
        q.append(LiveEvent(session_id=session_id, payload=payload))

    def drain(self, session_id: str) -> list[dict]:
        """Return and clear all pending events for *session_id*."""
        q = self._queues.pop(session_id, None)
        if q is None:
            return []
        return [e.payload for e in q]

    def size(self, session_id: str) -> int:
        q = self._queues.get(session_id)
        return len(q) if q else 0

    def clear(self, session_id: str) -> None:
        self._queues.pop(session_id, None)
