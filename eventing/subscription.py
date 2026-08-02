"""
G5: Subscription — exact scope, no None/Global catch-all, duplicate detection.

- scope is REQUIRED (no default None)
- Duplicate (event_type, subscriber_id, scope) → DuplicateSubscriptionError
- close() sets closed flag — bus removes from subscriber lists
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.eventing.scope import ScopeToken


class SubscriptionClosedError(RuntimeError):
    """Subscription has already been closed."""


class DuplicateSubscriptionError(RuntimeError):
    """A subscription with the same (event_type, subscriber_id, scope) already exists."""


class Subscription:
    """Opaque subscription handle.  close() is idempotent.

    Tracks subscription lifecycle.  The bus cleans up subscriber lists
    when a subscription is closed.
    """

    __slots__ = ("_event_type", "_subscriber_id", "_closed", "_scope")

    def __init__(
        self,
        event_type: str,
        subscriber_id: str,
        scope: ScopeToken,
    ) -> None:
        self._event_type = event_type
        self._subscriber_id = subscriber_id
        self._closed = False
        self._scope = scope

    @property
    def event_type(self) -> str:
        return self._event_type

    @property
    def subscriber_id(self) -> str:
        return self._subscriber_id

    @property
    def scope(self) -> ScopeToken:
        return self._scope

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Idempotent close — second call is a no-op, not an error."""
        self._closed = True

    @property
    def identity(self) -> tuple[str, str, str, str | None, str | None, int]:
        """Unique identity: (event_type, subscriber_id, kind, session_id, task_id, generation)."""
        return (
            self._event_type,
            self._subscriber_id,
            self._scope.kind.value,
            str(self._scope.session_id) if self._scope.session_id else None,
            str(self._scope.task_id) if self._scope.task_id else None,
            self._scope.generation,
        )

    def __repr__(self) -> str:
        return (
            f"Subscription({self._event_type!r}, id={self._subscriber_id!r}, "
            f"scope={self._scope.kind.value}, gen={self._scope.generation})"
        )
