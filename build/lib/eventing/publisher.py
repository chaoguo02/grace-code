"""
P4: Publisher Protocol — typed event publishing contract.

Only defines the interface.  Does NOT import application event schemas.
"""

from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable

from application.events.envelope import EventEnvelope

PayloadT = TypeVar("PayloadT", covariant=True)


class PublishError(RuntimeError):
    """Raised when an event cannot be published."""


class ScopeClosedError(PublishError):
    """Scope has been closed — no further events accepted."""


class PayloadRejectedError(PublishError):
    """Payload type not in schema registry."""


@runtime_checkable
class EventPublisher(Protocol[PayloadT]):
    """Typed publisher for one event type.

    Usage:
        run_publisher: EventPublisher[RunCompletedV1]
        run_publisher.publish(envelope)
    """

    def publish(self, envelope: EventEnvelope[PayloadT]) -> None:
        """Publish an event to all subscribers of this type.

        Raises:
            ScopeClosedError: the target scope is no longer accepting events.
            PayloadRejectedError: payload class not registered.
        """
        ...

    def is_accepting(self) -> bool:
        """True if the target scope is still open."""
        ...
