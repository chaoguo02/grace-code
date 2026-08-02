"""
G7: Subscriber Protocol — typed event consumption, zero business imports.

HandlerOutcome replaces DeliveryReceipt in the port layer.
Subscriber implementations return HandlerOutcome (Accepted | Rejected | Failed).
Variance fixed: PayloadT is contravariant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Protocol, TypeVar, runtime_checkable

PayloadT = TypeVar("PayloadT", contravariant=True)


# ── HandlerOutcome — receipt at the port boundary ──────────────────────────

@dataclass(frozen=True, slots=True)
class Accepted:
    """Handler successfully processed the event."""
    subscriber_id: str = ""


@dataclass(frozen=True, slots=True)
class Rejected:
    """Handler rejected the event (e.g. wrong type version)."""
    subscriber_id: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class HandlerFailed:
    """Handler raised an exception."""
    subscriber_id: str = ""
    error: str = ""


HandlerOutcome = Accepted | Rejected | HandlerFailed


# ── Backward-compatible DeliveryReceipt ────────────────────────────────────
# Kept until G8+ listeners are migrated to HandlerOutcome.


class DeliveryReceipt:
    """Backward-compatible receipt.  Prefer HandlerOutcome for new code.

    G7 keeps this export so existing listeners (trace, stats, ws_gateway)
    continue to work until their migration phases.
    """

    __slots__ = ("_event_id", "_subscriber_id", "_success")

    def __init__(self, event_id: str, subscriber_id: str, success: bool) -> None:
        self._event_id = event_id
        self._subscriber_id = subscriber_id
        self._success = success

    @property
    def event_id(self) -> str:
        return self._event_id

    @property
    def subscriber_id(self) -> str:
        return self._subscriber_id

    @property
    def success(self) -> bool:
        return self._success

    @classmethod
    def ok(cls, event_id: str, subscriber_id: str) -> DeliveryReceipt:
        return cls(event_id, subscriber_id, True)

    @classmethod
    def failed(cls, event_id: str, subscriber_id: str) -> DeliveryReceipt:
        return cls(event_id, subscriber_id, False)


# ── Subscriber ─────────────────────────────────────────────────────────────

@runtime_checkable
class EventSubscriber(Protocol[PayloadT]):
    """Typed subscriber for one event type — sync handler."""

    def on_event(self, message) -> HandlerOutcome:
        """Process an event.  MUST return a HandlerOutcome.

        Exceptions caught by the bus → HandlerFailed.
        """
        ...


@runtime_checkable
class AsyncEventSubscriber(Protocol[PayloadT]):
    """Async variant — for subscribers that need I/O."""

    async def on_event(self, message) -> HandlerOutcome:
        ...
