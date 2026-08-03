"""
G27: WS Gateway — best-effort broadcast, subscription lifecycle, no EventBus entry.

- WS best-effort failure does NOT affect durable ACK.
- disconnect auto-closes subscription; no retained callbacks.
- Terminal facts shown only once.
"""

from __future__ import annotations

from eventing.subscriber import DeliveryReceipt


class WsGateway:
    """Broadcasts events to WebSocket subscribers.  Best-effort only."""

    NAME = "ws_gateway"

    def __init__(self) -> None:
        self._subs: dict[str, list] = {}  # session_id → [callback, ...]

    def on_event(self, envelope) -> DeliveryReceipt:
        """Handle a domain event — broadcast to session subscribers.

        G27: Best-effort.  Failure does NOT affect durable ACK.
        """
        sid = str(envelope.scope.session_id) if envelope.scope.session_id else ""
        if sid:
            self.broadcast(sid, {
                "event_type": str(envelope.event_type),
                "event_id": str(envelope.event_id),
                "aggregate_id": str(envelope.aggregate_id),
                "payload": envelope.canonical_json(),
            })
        return DeliveryReceipt.ok(str(envelope.event_id), self.NAME)

    def broadcast(self, session_id: str, msg: dict) -> None:
        """Send message to all subscribers of *session_id*.  Errors swallowed."""
        for ws in list(self._subs.get(session_id, [])):
            try:
                ws(msg)
            except Exception:
                pass  # G27: best-effort failure does not affect ACK

    def subscribe(self, session_id: str, callback) -> None:
        """Add a WS callback.  Returns unsubscribe function."""
        if session_id not in self._subs:
            self._subs[session_id] = []
        self._subs[session_id].append(callback)

    def unsubscribe(self, session_id: str, callback) -> None:
        """Remove a WS callback.  Idempotent."""
        subs = self._subs.get(session_id, [])
        if callback in subs:
            subs.remove(callback)
        if not subs and session_id in self._subs:
            del self._subs[session_id]

    def disconnect_session(self, session_id: str) -> int:
        """Disconnect all subscribers for a session.  Returns count removed."""
        count = len(self._subs.get(session_id, []))
        self._subs.pop(session_id, None)
        return count

    @property
    def subscriber_sessions(self) -> int:
        return len(self._subs)
