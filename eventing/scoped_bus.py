"""
P5: ScopedEventBus — exact-scope event routing.

No bubbling.  No async (sync bus for now — P6 adds async).
Does NOT import business schemas.
"""

from __future__ import annotations

import logging
from typing import Callable

from application.events.envelope import EventEnvelope
from core.eventing.identifiers import SessionId, TaskId
from core.eventing.scope import ScopeKind
from eventing.scope_tree import ScopeTree, ScopeClosedError, ScopeNotFoundError
from eventing.subscription import Subscription

logger = logging.getLogger(__name__)


class ScopedEventBus:
    """Synchronous event bus with exact-scope routing.

    Usage:
        bus = ScopedEventBus(global_id)
        bus.ensure_session(sid, gen=1)
        sub = bus.subscribe("run.completed.v1", handler, scope=sid)
        bus.publish(envelope)  # routes by envelope.scope
        sub.close()
        bus.close_session(sid)
    """

    def __init__(self, global_id=None) -> None:
        self._tree = ScopeTree(global_id)
        self._subscribers: dict[str, list[tuple[Subscription, Callable]]] = {}

    # ── Scope management ────────────────────────────────────────────────

    def ensure_session(self, session_id: SessionId, generation: int = 0) -> None:
        self._tree.ensure_session(session_id, generation)

    def ensure_task(
        self, session_id: SessionId, task_id: TaskId, generation: int = 0,
    ) -> None:
        self._tree.ensure_task(session_id, task_id, generation)

    def close_session(self, session_id: SessionId) -> None:
        self._tree.close_session(session_id)

    def close_task(self, session_id: SessionId, task_id: TaskId) -> None:
        self._tree.close_task(session_id, task_id)

    def close_all(self) -> None:
        self._tree.close_all()

    # ── Subscribe ───────────────────────────────────────────────────────

    def subscribe(
        self,
        event_type: str,
        handler: Callable[[EventEnvelope], None],
        subscriber_id: str = "",
    ) -> Subscription:
        """Subscribe to *event_type*.  Returns handle for unsubscribe."""
        sub = Subscription(event_type, subscriber_id or str(id(handler)))
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append((sub, handler))
        return sub

    # ── Publish ─────────────────────────────────────────────────────────

    def publish(self, envelope: EventEnvelope) -> None:
        """Route *envelope* to matching subscribers by exact scope."""
        scope = envelope.scope
        event_type = str(envelope.event_type)

        # Reject closed scopes
        node = self._tree.find(scope)
        if node is None:
            raise ScopeNotFoundError(
                f"No scope found for {scope.kind.value}:{scope.generation}"
            )
        if node.closed:
            raise ScopeClosedError(
                f"Scope {scope.kind.value}:{scope.generation} is closed"
            )
        # Reject stale generations
        if scope.kind != ScopeKind.GLOBAL and scope.generation < node.generation:
            raise ScopeClosedError(
                f"Scope generation {scope.generation} is stale "
                f"(current: {node.generation})"
            )

        # Deliver to matching subscribers
        for _sub, handler in self._subscribers.get(event_type, []):
            if _sub.closed:
                continue
            try:
                handler(envelope)
            except Exception:
                logger.debug(
                    "Subscriber %s failed for %s",
                    _sub.subscriber_id, event_type, exc_info=True,
                )

    @property
    def subscriber_count(self) -> int:
        return sum(len(v) for v in self._subscribers.values())
