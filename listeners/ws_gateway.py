"""P16: WS Gateway — best-effort broadcast, no backpressure on outbox."""
from __future__ import annotations

from eventing.subscriber import DeliveryReceipt


class WsGateway:
    NAME = "ws_gateway"

    def __init__(self, subscribers: dict | None = None) -> None:
        self._subs: dict[str, list] = {}
        if subscribers:
            self._subs.update({k: list(v) for k, v in subscribers.items()})

    def on_event(self, envelope) -> DeliveryReceipt:
        """Handle a domain event from the EventBus.

        Broadcasts to all WebSocket subscribers of the event's session.
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
        for ws in self._subs.get(session_id, []):
            try:
                ws(msg)
            except Exception:
                pass

    def subscribe(self, session_id: str, callback) -> None:
        self._subs.setdefault(session_id, []).append(callback)

    def unsubscribe(self, session_id: str, callback) -> None:
        subs = self._subs.get(session_id, [])
        if callback in subs:
            subs.remove(callback)
