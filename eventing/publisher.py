"""
G7: Publisher Protocol — typed event publishing, zero business imports.

Defines ScopedMessage as a generic carrier so the EventBus layer never
imports application.events.envelope or any business schema.
"""

from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable

from core.eventing.scope import ScopeToken

PayloadT_co = TypeVar("PayloadT_co", covariant=True)


# ── ScopedMessage — generic envelope (no business schema) ──────────────────

@runtime_checkable
class ScopedMessage(Protocol[PayloadT_co]):
    """Generic event carrier — the only envelope type visible to EventBus.

    The eventing layer never imports concrete envelope modules.
    """

    @property
    def event_type(self) -> str: ...

    @property
    def scope(self) -> ScopeToken: ...

    @property
    def payload(self) -> PayloadT_co: ...


# ── Errors ──────────────────────────────────────────────────────────────────

class PublishError(RuntimeError):
    """Raised when an event cannot be published."""


class ScopeClosedError(PublishError):
    """Scope has been closed — no further events accepted."""


class PayloadRejectedError(PublishError):
    """Payload type not in schema registry."""


# ── Publisher ───────────────────────────────────────────────────────────────

@runtime_checkable
class EventPublisher(Protocol[PayloadT_co]):
    """Typed publisher for one event type.

    Usage:
        run_publisher: EventPublisher[RunCompletedV1]
        run_publisher.publish(message)
    """

    def publish(self, message: ScopedMessage[PayloadT_co]) -> None:
        """Publish an event to all subscribers of this type.

        Raises:
            ScopeClosedError: the target scope is no longer accepting events.
            PayloadRejectedError: payload not in schema registry.
        """
        ...

    def is_accepting(self) -> bool:
        """True if the target scope is still open."""
        ...
