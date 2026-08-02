"""P16: WS Gateway — best-effort broadcast, no backpressure on outbox."""
from __future__ import annotations

class WsGateway:
    def __init__(self, subscribers: dict | None = None) -> None:
        self._subs: dict[str, list] = {}
        if subscribers:
            self._subs.update({k: list(v) for k, v in subscribers.items()})

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
