"""
G5: ScopedEventBus — exact-scope routing, no None/Global catch-all.

- subscribe() requires an EXACT ScopeToken (no default None).
- _scope_matches() uses FULL ScopeToken equality.
- GLOBAL subscriber receives ONLY GLOBAL events.
- SESSION subscriber receives ONLY exact-session SESSION events.
- TASK subscriber receives ONLY exact-task TASK events.
- No implicit bubbling, no cross-scope delivery.
- Duplicate (event_type, subscriber_id, scope) → DuplicateSubscriptionError.
- Subscription.close() immediately removes from subscriber lists.
- subscriber_count excludes closed subscriptions.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from core.eventing.identifiers import SessionId, TaskId
from core.eventing.scope import ScopeKind, ScopeToken
from eventing.bounded_channel import BoundedChannel
from eventing.publisher import ScopedMessage
from eventing.scope_tree import ScopeTree, ScopeClosedError, ScopeNotFoundError, StaleGenerationError
from eventing.subscription import Subscription, DuplicateSubscriptionError

logger = logging.getLogger(__name__)


def _scope_matches(sub_scope: ScopeToken, event_scope: ScopeToken) -> bool:
    """G5: Exact scope matching — full ScopeToken equality.

    - GLOBAL subscriber ← ONLY GLOBAL events (same global_id + generation)
    - SESSION subscriber ← ONLY same-session SESSION events
    - TASK subscriber ← ONLY same-task TASK events
    - No cross-kind delivery (GLOBAL does NOT receive SESSION/TASK)
    """
    return sub_scope == event_scope


class ScopedEventBus:
    """Event bus with EXACT-scope routing.

    Usage:
        bus = ScopedEventBus()
        bus.ensure_session(sid, gen=1)
        scope = bus._tree.ensure_session(sid, gen=1).token
        sub = bus.subscribe("run.completed.v1", handler, "trace", scope=scope)
        bus.publish(envelope)  # routes by exact envelope.scope
        sub.close()
        bus.close_session(sid)
    """

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

    def close_all(self) -> None:
        self._tree.close_all()

    # ── Internal helpers ────────────────────────────────────────────────

    def _remove_scoped_subscriptions(
        self,
        session_id: SessionId | None = None,
        task_id: TaskId | None = None,
    ) -> None:
        """Remove all subscriptions whose scope matches the closed session/task.

        G5: Uses exact scope matching — the scope token from the closed node
        must match the subscription's scope token exactly.
        """
        removed = 0
        for event_type in list(self._subscribers.keys()):
            kept: list[tuple[Subscription, Callable]] = []
            for sub, handler in self._subscribers[event_type]:
                if sub.closed:
                    removed += 1
                    continue
                sub_scope = sub.scope
                if task_id is not None and sub_scope.kind == ScopeKind.TASK:
                    if sub_scope.session_id == session_id and sub_scope.task_id == task_id:
                        sub.close()
                        removed += 1
                        continue
                elif session_id is not None and sub_scope.kind == ScopeKind.SESSION:
                    if sub_scope.session_id == session_id:
                        sub.close()
                        removed += 1
                        continue
                kept.append((sub, handler))
            if kept:
                self._subscribers[event_type] = kept
            else:
                del self._subscribers[event_type]

        # Clean up sub_ids
        self._sub_ids = {
            sid for sid in self._sub_ids
            if not (task_id is not None and sid[3] == str(task_id))
            and not (session_id is not None and sid[2] == str(session_id)
                     and task_id is None and sid[3] is None)
        }

    # ── Subscribe ───────────────────────────────────────────────────────

    def subscribe(
        self,
        event_type: str,
        handler: Callable[[ScopedMessage], None],
        subscriber_id: str = "",
        *,
        scope: ScopeToken,
    ) -> Subscription:
        """Subscribe to *event_type* for EXACT *scope*.

        G5: scope is REQUIRED.  No None → Global catch-all.
        Raises DuplicateSubscriptionError if (event_type, subscriber_id, scope)
        is already registered.
        """
        sub = Subscription(
            event_type, subscriber_id or str(id(handler)), scope=scope,
        )

        # G5: Duplicate detection
        dup_key = (event_type, sub.subscriber_id, scope.identity)
        if dup_key in self._sub_ids:
            raise DuplicateSubscriptionError(
                f"Duplicate subscription: event_type={event_type!r}, "
                f"subscriber_id={sub.subscriber_id!r}, "
                f"scope={scope.kind.value}:gen={scope.generation}"
            )
        self._sub_ids.add(dup_key)

        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append((sub, handler))
        return sub

    # ── Publish (sync) ──────────────────────────────────────────────────

    def publish(self, message: ScopedMessage) -> None:
        """Route *message* to subscribers with EXACT matching scope.

        Synchronous delivery.  Handler exceptions are logged but swallowed.
        For async delivery with backpressure, use async_publish().
        """
        scope = message.scope
        event_type = str(message.event_type)

        self._validate_scope(scope)

        for _sub, handler in self._subscribers.get(event_type, []):
            if _sub.closed:
                continue
            if not _scope_matches(_sub.scope, scope):
                continue
            try:
                handler(message)
            except Exception:
                logger.debug(
                    "Subscriber %s failed for %s",
                    _sub.subscriber_id, event_type, exc_info=True,
                )

    # ── G6: Async publish with backpressure ──────────────────────────────

    def __init__(self, global_id=None) -> None:
        self._tree = ScopeTree(global_id)
        self._subscribers: dict[str, list[tuple[Subscription, Callable]]] = {}
        self._sub_ids: set[tuple] = set()
        # G6: Async delivery infrastructure
        self._channels: dict[str, BoundedChannel] = {}
        self._channel_tasks: set[asyncio.Task] = set()
        self._delivery_errors: list[dict] = []
        self._async_started = False

    async def async_publish(
        self,
        message: ScopedMessage,
        channel_capacity: int = 256,
        timeout: float | None = None,
    ):
        """Publish *message* asynchronously through a bounded channel.

        Each scope gets its own channel.  Delivery is FIFO with backpressure.
        Returns DeliveryReceipt.
        """
        import asyncio
        from eventing.bounded_channel import (
            BoundedChannel, Delivered, RejectedClosed,
        )

        scope = message.scope
        scope_key = scope.scope_key

        self._validate_scope(scope)

        # Get or create channel for this scope
        if scope_key not in self._channels:
            channel = BoundedChannel(
                capacity=channel_capacity,
                consumer_handler=lambda m: self._deliver_to_subscribers(m),
                channel_id=scope_key,
                handler_timeout_s=30.0,
                error_sink=self._delivery_errors,
            )
            channel.start_consumer()
            self._channels[scope_key] = channel

        channel = self._channels[scope_key]
        receipt = await channel.put(message, timeout=timeout)
        return receipt

    async def _deliver_to_subscribers(self, message: ScopedMessage) -> None:
        """Internal: fan out *message* to all matching subscribers."""
        event_type = str(message.event_type)
        scope = message.scope

        for _sub, handler in self._subscribers.get(event_type, []):
            if _sub.closed:
                continue
            if not _scope_matches(_sub.scope, scope):
                continue
            # Call handler — exception propagates to channel's consumer loop
            result = handler(message)
            # If handler is a coroutine, await it
            if hasattr(result, '__await__'):
                await result

    async def start_async(self) -> None:
        """Start async delivery infrastructure.  Idempotent."""
        if self._async_started:
            return
        self._async_started = True

    async def shutdown_async(self, drain_timeout_s: float = 5.0) -> int:
        """Structured async shutdown: drain all channels, cancel consumers.

        Returns total undelivered items across all channels.
        """
        import asyncio

        total_remaining = 0
        for scope_key, channel in list(self._channels.items()):
            remaining = await channel.shutdown(drain_timeout_s=drain_timeout_s)
            total_remaining += remaining

        # Cancel any orphaned tasks
        for task in list(self._channel_tasks):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._channels.clear()
        self._channel_tasks.clear()
        self._async_started = False
        return total_remaining

    @property
    def delivery_errors(self) -> list[dict]:
        """Error sink — handler failures during async delivery."""
        return list(self._delivery_errors)

    @property
    def channel_count(self) -> int:
        return len(self._channels)

    # ── Internal helpers ────────────────────────────────────────────────

    def _validate_scope(self, scope: ScopeToken) -> None:
        """Raise if *scope* is closed, stale, or not found."""
        node = self._tree.find(scope)
        if node is None:
            raise ScopeNotFoundError(
                f"No scope found for {scope.kind.value}:gen={scope.generation}"
            )
        if node.closed:
            raise ScopeClosedError(
                f"Scope {scope.kind.value}:gen={scope.generation} is closed"
            )
        if scope.kind != ScopeKind.GLOBAL and scope.generation < node.generation:
            raise ScopeClosedError(
                f"Scope generation {scope.generation} is stale "
                f"(current: {node.generation})"
            )

    @property
    def subscriber_count(self) -> int:
        """Count of non-closed subscriptions."""
        return sum(
            1 for subs in self._subscribers.values()
            for sub, _h in subs if not sub.closed
        )
