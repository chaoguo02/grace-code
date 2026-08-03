"""P17: Stats Projection — records run metrics from domain events."""
from __future__ import annotations

from eventing.subscriber import DeliveryReceipt


class StatsProjection:
    NAME = "stats_projection"

    def __init__(self) -> None:
        self._metrics: list[dict] = []

    def on_event(self, envelope) -> DeliveryReceipt:
        et = str(envelope.event_type)
        if et.startswith("run."):
            self._metrics.append({
                "event_type": et,
                "session_id": str(envelope.scope.session_id) if envelope.scope.session_id else "",
                "aggregate_id": str(envelope.aggregate_id),
                "occurred_at": envelope.occurred_at.isoformat(),
            })
        return DeliveryReceipt.ok(str(envelope.event_id), self.NAME)

    @property
    def metrics(self) -> list[dict]:
        return list(self._metrics)
