"""Durable outbox projection pipeline.

Trace persistence is authoritative and transactional. Live WebSocket delivery
is a best-effort projection of an already durable fact.
"""

from __future__ import annotations

from collections.abc import Callable

from server.projections.trace_projection import TraceProjection
from server.ws.event_mapper import map_domain_to_ws


class UnsupportedEventVersion(ValueError):
    pass


class ProjectionRunner:
    """Project a claimed OutboxRecord before the relay acknowledges it."""

    def __init__(
        self,
        trace_projection: TraceProjection,
        publish_live: Callable[[str, dict], None] | None = None,
    ) -> None:
        self._trace_projection = trace_projection
        self._publish_live = publish_live

    def deliver(self, record) -> None:
        if record.event_version != 1:
            raise UnsupportedEventVersion(
                f"unsupported {record.event_type} version {record.event_version}"
            )

        projected = self._trace_projection.project(record)
        if not projected:
            return

        message = map_domain_to_ws(record)
        if message is not None and self._publish_live is not None:
            # Live delivery is intentionally not persisted a second time.
            self._publish_live(record.session_id, message)
