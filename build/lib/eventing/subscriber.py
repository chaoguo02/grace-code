"""
P4: Subscriber Protocol — typed event consumption contract.
"""

from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable

PayloadT = TypeVar("PayloadT", contravariant=True)


class DeliveryReceipt:
    """Proof that a subscriber received and processed an event.

    Immutable after construction.
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


@runtime_checkable
class EventSubscriber(Protocol[PayloadT]):
    """Typed subscriber for one event type.

    Usage:
        class TraceProjection:
            def on_event(self, envelope: EventEnvelope[RunCompletedV1]) -> DeliveryReceipt:
                ...
    """

    def on_event(self, envelope) -> DeliveryReceipt:
        """Process an event.  MUST return a DeliveryReceipt.

        Exceptions are caught by the bus and result in DeliveryReceipt.failed().
        """
        ...


@runtime_checkable
class AsyncEventSubscriber(Protocol[PayloadT]):
    """Async variant — for subscribers that need I/O."""

    async def on_event(self, envelope) -> DeliveryReceipt:
        ...
