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
from core.eventing.scope import ScopeKind, ScopeToken
from eventing.scope_tree import ScopeTree, ScopeClosedError, ScopeNotFoundError
from eventing.subscription import Subscription

logger = logging.getLogger(__name__)


def _scope_matches(
    sub_scope: ScopeToken | None, event_scope: ScopeToken,
) -> bool:
    """Check whether a subscriber's scope matches an event's scope.

    - None / GLOBAL: receives ALL events (backward compat)
    - SESSION: only events from same session_id
    - TASK: only events from same session_id AND task_id
    """
    if sub_scope is None:
        return True
    if sub_scope.kind == ScopeKind.GLOBAL:
        return True
    if sub_scope.kind == ScopeKind.SESSION:
        return event_scope.session_id == sub_scope.session_id
    if sub_scope.kind == ScopeKind.TASK:
        return (
            event_scope.session_id == sub_scope.session_id
            and event_scope.task_id == sub_scope.task_id
        )
    return False


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
        self._remove_scoped_subscriptions(session_id=session_id)

    def close_task(self, session_id: SessionId, task_id: TaskId) -> None:
        self._tree.close_task(session_id, task_id)
        self._remove_scoped_subscriptions(
            session_id=session_id, task_id=task_id,
        )

    # ── Internal helpers ──────────────────────────────────────────────────

    def _remove_scoped_subscriptions(
        self,
        session_id: SessionId | None = None,
        task_id: TaskId | None = None,
    ) -> None:
        """Remove all subscriptions scoped to a given session or task."""
        for event_type in list(self._subscribers.keys()):
            kept = []
            for _sub, handler in self._subscribers[event_type]:
                sub_scope = _sub.scope
                if sub_scope is None or sub_scope.kind == ScopeKind.GLOBAL:
                    # GLOBAL-scope subscribers survive scope close
                    kept.append((_sub, handler))
                    continue
                if task_id is not None and sub_scope.kind == ScopeKind.TASK:
                    if (
                        sub_scope.session_id == session_id
                        and sub_scope.task_id == task_id
                    ):
                        continue  # drop this subscription
                elif (
                    session_id is not None
                    and sub_scope.session_id == session_id
                ):
                    continue  # drop this subscription
                kept.append((_sub, handler))
            if kept:
                self._subscribers[event_type] = kept
            else:
                del self._subscribers[event_type]

    def close_all(self) -> None:
        self._tree.close_all()

    # ── Subscribe ───────────────────────────────────────────────────────

    def subscribe(
        self,
        event_type: str,
        handler: Callable[[EventEnvelope], None],
        subscriber_id: str = "",
        scope: ScopeToken | None = None,
    ) -> Subscription:
        """Subscribe to *event_type*.  Returns handle for unsubscribe.

        *scope* restricts delivery:
        - None or GLOBAL → receives all events (backward compat)
        - SESSION → only events from the same session
        - TASK → only events from the same session AND task
        """
        sub = Subscription(
            event_type, subscriber_id or str(id(handler)), scope=scope,
        )
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

        # Deliver to matching subscribers (scope + event_type)
        for _sub, handler in self._subscribers.get(event_type, []):
            if _sub.closed:
                continue
            if not _scope_matches(_sub.scope, scope):
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
