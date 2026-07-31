"""Headless approval broker registry — per-session ApprovalBroker management.

Extracted from SessionRuntime._ensure_approval_broker / get_approval_broker
in Phase 4b.  This is a transport-layer concern (HTTP/WebSocket headless
approval), not a session runtime concern.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from server.services.approval_broker import ApprovalBroker


class HeadlessApprovalService:
    """Per-session ApprovalBroker registry.

    One broker per session.  The agent thread blocks on
    ``broker.wait_for_decision()``; the HTTP handler resolves via
    ``broker.resolve()``.  This is the exact same synchronous-blocking
    pattern as CC's stdin ``control_response``.
    """

    def __init__(self) -> None:
        self._brokers: dict[str, "ApprovalBroker"] = {}
        self._lock = threading.Lock()

    def get_or_create(self, session_id: str) -> "ApprovalBroker":
        """Get or create the per-session ApprovalBroker."""
        with self._lock:
            if session_id not in self._brokers:
                from server.services.approval_broker import ApprovalBroker
                self._brokers[session_id] = ApprovalBroker(session_id)
            return self._brokers[session_id]

    def get(self, session_id: str) -> "ApprovalBroker | None":
        """Return the ApprovalBroker for *session_id*, if one exists."""
        with self._lock:
            return self._brokers.get(session_id)

    def remove(self, session_id: str) -> None:
        """Remove the broker for *session_id* (cleanup)."""
        with self._lock:
            self._brokers.pop(session_id, None)

    def clear(self) -> None:
        """Remove all brokers (shutdown)."""
        with self._lock:
            self._brokers.clear()
