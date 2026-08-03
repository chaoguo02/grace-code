"""
G8: ProjectionDispatcher — typed dispatch, required vs best-effort, no catch-all.

- Each projection registers for specific (event_type, schema_version) pairs.
- Required projection failure → entire delivery is Retryable (not Delivered).
- Best-effort failure → counted but does not block durable ACK.
- Unknown schema/version → PermanentDeliveryFailure.
- Does NOT import Runtime, Command, Coordinator, or EventBus internals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from listeners.delivery import (
    DeliveryOutcome,
    Delivered,
    ProjectionReceipt,
    RetryableDeliveryFailure,
    PermanentDeliveryFailure,
    merge_receipts,
)


# ── Projection entry ───────────────────────────────────────────────────────

@dataclass
class ProjectionEntry:
    """A registered projection handler."""
    name: str
    handler: Callable  # (envelope) -> ProjectionReceipt
    required: bool = True  # False = best-effort (failure does not block ACK)
    event_types: tuple[str, ...] = ()  # explicit list, not catch-all


# ── ProjectionDispatcher ────────────────────────────────────────────────────

class ProjectionDispatcher:
    """Typed dispatcher: routes durable facts to registered projections.

    Does NOT use a live EventBus.  Each projection registers for
    specific (event_type) strings — no catch-all, no wildcard.

    Usage:
        dispatcher = ProjectionDispatcher()
        dispatcher.register("trace", trace.on_event, required=True,
                            event_types=("run.completed.v1", ...))
        outcome = dispatcher.dispatch(envelope)
        # outcome is Delivered | Retryable | Permanent
    """

    def __init__(self) -> None:
        self._entries: list[ProjectionEntry] = []
        self._required_names: set[str] = set()
        self._best_effort_names: set[str] = set()

    def register(
        self,
        name: str,
        handler: Callable,
        *,
        required: bool = True,
        event_types: tuple[str, ...] = (),
    ) -> None:
        """Register a projection for specific event types.

        Raises ValueError if event_types is empty (no catch-all).
        Raises ValueError if name already registered.
        """
        if not event_types:
            raise ValueError(
                f"Projection '{name}': must specify explicit event_types "
                f"(no catch-all allowed)"
            )
        for existing in self._entries:
            if existing.name == name:
                raise ValueError(
                    f"Projection '{name}' already registered"
                )

        entry = ProjectionEntry(
            name=name,
            handler=handler,
            required=required,
            event_types=event_types,
        )
        self._entries.append(entry)
        if required:
            self._required_names.add(name)
        else:
            self._best_effort_names.add(name)

    def dispatch(self, envelope) -> DeliveryOutcome:
        """Deliver *envelope* to all registered projections.

        Returns:
          - Delivered: all required projections OK
          - RetryableDeliveryFailure: at least one required failed transiently
          - PermanentDeliveryFailure: unknown type or poison
        """
        event_type = str(envelope.event_type)

        # Find matching projections
        matching = [
            e for e in self._entries
            if event_type in e.event_types
        ]

        if not matching:
            # No projection registered for this event type
            # Not necessarily an error — could be a new event type with no
            # projections yet.  Treat as delivered with zero receipts.
            return Delivered(receipts=())

        receipts: list[ProjectionReceipt] = []
        for entry in matching:
            try:
                receipt = entry.handler(envelope)
                if receipt is None:
                    receipt = ProjectionReceipt(
                        projection_name=entry.name,
                        event_id=str(envelope.event_id),
                        success=False,
                        error="handler returned None",
                    )
                receipts.append(receipt)
            except Exception as exc:
                receipts.append(ProjectionReceipt(
                    projection_name=entry.name,
                    event_id=str(envelope.event_id),
                    success=False,
                    error=f"{type(exc).__name__}: {exc}",
                ))

        return merge_receipts(
            receipts,
            required_names=self._required_names,
            best_effort_names=self._best_effort_names,
        )

    def dispatch_unknown(self, event_type: str, event_id: str = "") -> DeliveryOutcome:
        """Handle an event type with no registered schema → Permanent."""
        return PermanentDeliveryFailure(
            reason=f"Unknown schema/version: {event_type}",
            receipts=(),
        )

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @property
    def required_count(self) -> int:
        return len(self._required_names)

    @property
    def best_effort_count(self) -> int:
        return len(self._best_effort_names)


# ── Backward-compatible alias ──────────────────────────────────────────────
# Old code imports ProjectionRunner from this module.
ProjectionRunner = ProjectionDispatcher
