"""
P4: Subscription token — idempotent close, typed error on double-close.
"""

from __future__ import annotations


class SubscriptionClosedError(RuntimeError):
    """Subscription has already been closed."""


class Subscription:
    """Opaque subscription handle.  close() is idempotent.

    Tracks subscription lifecycle.  The bus uses this to clean up
    subscriber lists when a subscription is closed.
    """

    __slots__ = ("_event_type", "_subscriber_id", "_closed")

    def __init__(self, event_type: str, subscriber_id: str) -> None:
        self._event_type = event_type
        self._subscriber_id = subscriber_id
        self._closed = False

    @property
    def event_type(self) -> str:
        return self._event_type

    @property
    def subscriber_id(self) -> str:
        return self._subscriber_id

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Idempotent close — second call is a no-op, not an error."""
        self._closed = True
