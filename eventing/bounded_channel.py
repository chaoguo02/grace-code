"""
G6: Bounded async channel — backpressure, task_done/join, structured shutdown.

- Capacity > 0 enforced at construction.
- put() returns DeliveryReceipt (Delivered | RejectedClosed | Backpressured | TimedOut).
- Consumer runs in a managed asyncio.Task; drain uses queue.task_done + join.
- close() → reject new publishes → poison sentinel → join pending → cancel consumer.
- No daemon threads, no fire-and-forget, no sleep(0.1) drain.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)


# ── Delivery Receipt ────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Delivered:
    """Item was accepted into the channel."""
    channel_id: str = ""


@dataclass(frozen=True, slots=True)
class RejectedClosed:
    """Channel is closed — item rejected."""
    channel_id: str = ""


@dataclass(frozen=True, slots=True)
class Backpressured:
    """Channel is full and publisher chose not to wait."""
    channel_id: str = ""
    capacity: int = 0
    size: int = 0


@dataclass(frozen=True, slots=True)
class TimedOut:
    """Publisher timed out waiting for channel space."""
    channel_id: str = ""
    timeout_s: float = 0.0


DeliveryReceipt = Delivered | RejectedClosed | Backpressured | TimedOut


# ── Errors ──────────────────────────────────────────────────────────────────

class ChannelFullError(RuntimeError):
    """Publisher timed out waiting for space in the channel."""


class ChannelClosedError(RuntimeError):
    """Channel is closed — no more items accepted."""


# ── BoundedChannel ──────────────────────────────────────────────────────────

# Sentinel value to signal consumer shutdown
_POISON = object()


class BoundedChannel(Generic[T]):
    """Bounded async channel with backpressure and structured close.

    Each item delivered to a consumer handler.  Handler failures are
    logged to an error sink but do not crash the consumer task.
    """

    def __init__(
        self,
        capacity: int,
        consumer_handler,
        channel_id: str = "",
        handler_timeout_s: float | None = None,
        error_sink: list | None = None,
    ) -> None:
        if capacity <= 0:
            raise ValueError(f"Channel capacity must be > 0, got {capacity}")
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=capacity)
        self._capacity = capacity
        self._channel_id = channel_id
        self._handler = consumer_handler
        self._handler_timeout_s = handler_timeout_s
        self._error_sink: list = error_sink if error_sink is not None else []
        self._closed = False
        self._consumer_task: asyncio.Task | None = None
        self._drained = asyncio.Event()
        # G6: Semaphore tracks in-flight items (queued + processing)
        self._in_flight = asyncio.Semaphore(capacity)
        self._pending_count = 0

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def size(self) -> int:
        return self._queue.qsize()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def pending(self) -> int:
        return self._pending_count

    @property
    def consumer_running(self) -> bool:
        return self._consumer_task is not None and not self._consumer_task.done()

    # ── Lifecycle ───────────────────────────────────────────────────────

    def start_consumer(self) -> None:
        """Start the consumer task.  Must be called within a running event loop."""
        if self._consumer_task is not None and not self._consumer_task.done():
            return
        self._consumer_task = asyncio.create_task(self._consume_loop())

    async def put(
        self, item: T, timeout: float | None = None,
    ) -> DeliveryReceipt:
        """Put an item into the channel.

        Capacity controls total in-flight items (queued + processing).
        Returns DeliveryReceipt.
        """
        if self._closed:
            return RejectedClosed(channel_id=self._channel_id)

        # Acquire in-flight slot (backpressure on total in-flight, not just queue)
        if timeout is not None:
            try:
                await asyncio.wait_for(
                    self._in_flight.acquire(), timeout=timeout
                )
            except asyncio.TimeoutError:
                return TimedOut(
                    channel_id=self._channel_id,
                    timeout_s=timeout,
                )
        else:
            # Wait forever (or until close)
            acquire_task = asyncio.create_task(self._in_flight.acquire())
            while not acquire_task.done():
                if self._closed:
                    acquire_task.cancel()
                    try:
                        await acquire_task
                    except asyncio.CancelledError:
                        pass
                    return RejectedClosed(channel_id=self._channel_id)
                await asyncio.sleep(0)
            await acquire_task

        # Re-check closed after acquiring (may have closed during wait)
        if self._closed:
            self._in_flight.release()
            return RejectedClosed(channel_id=self._channel_id)

        await self._queue.put(item)
        self._pending_count += 1
        return Delivered(channel_id=self._channel_id)

    async def close(self) -> None:
        """Signal no more items.  Reject new puts."""
        self._closed = True

    async def drain(self, drain_timeout_s: float = 5.0) -> int:
        """Wait for pending items to be consumed.  Returns remaining count."""
        if self._pending_count == 0:
            return 0

        try:
            await asyncio.wait_for(self._queue.join(), timeout=drain_timeout_s)
        except asyncio.TimeoutError:
            pass

        remaining = self._queue.qsize()
        if remaining > 0:
            logger.warning(
                "Channel %s drain: %d items remaining after %.1fs",
                self._channel_id, remaining, drain_timeout_s,
            )
        return remaining

    async def shutdown(self, drain_timeout_s: float = 5.0) -> int:
        """Full shutdown: close → poison → drain → cancel consumer.

        Returns count of undelivered items.
        """
        # 1. Reject new publishes
        await self.close()

        # 2. Send poison pill to stop consumer after queue is drained
        try:
            self._queue.put_nowait(_POISON)
        except asyncio.QueueFull:
            pass  # queue is full, consumer will drain what it can

        # 3. Wait for pending items
        if self._consumer_task is not None and not self._consumer_task.done():
            remaining = await self.drain(drain_timeout_s=drain_timeout_s)
        else:
            remaining = self._queue.qsize()

        # 4. Cancel consumer task
        if self._consumer_task is not None and not self._consumer_task.done():
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass

        return remaining

    # ── Consumer loop ───────────────────────────────────────────────────

    async def _consume_loop(self) -> None:
        """Continuously consume items from the queue."""
        while True:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                break

            try:
                if item is _POISON:
                    self._queue.task_done()
                    break

                result = self._handler(item)
                # Handler may be sync or async — only await if awaitable.
                # (await on a sync callable's None raises TypeError, which
                # would silently release the semaphore and break backpressure.)
                if hasattr(result, '__await__'):
                    if self._handler_timeout_s is not None:
                        await asyncio.wait_for(
                            result, timeout=self._handler_timeout_s,
                        )
                    else:
                        await result

            except asyncio.TimeoutError:
                logger.warning(
                    "Channel %s: handler timed out after %.1fs",
                    self._channel_id, self._handler_timeout_s,
                )
                self._error_sink.append({
                    "channel_id": self._channel_id,
                    "error": "handler_timeout",
                    "timeout_s": self._handler_timeout_s,
                })
            except asyncio.CancelledError:
                # finally block handles task_done + semaphore release
                break
            except Exception as exc:
                logger.debug(
                    "Channel %s: handler error: %s",
                    self._channel_id, exc, exc_info=True,
                )
                self._error_sink.append({
                    "channel_id": self._channel_id,
                    "error": str(exc),
                })
            finally:
                try:
                    self._queue.task_done()
                    self._pending_count = max(0, self._pending_count - 1)
                except ValueError:
                    pass
                if item is not _POISON:
                    self._in_flight.release()

        self._drained.set()
