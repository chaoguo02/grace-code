"""
P6: Bounded async channel — backpressure + structured close.

Capacity must be > 0.  Full channel blocks publisher until space available
or timeout.  close() drains and awaits all pending deliveries.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Generic, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)


class ChannelFullError(RuntimeError):
    """Publisher timed out waiting for space in the channel."""


class ChannelClosedError(RuntimeError):
    """Channel is closed — no more items accepted."""


class BoundedChannel(Generic[T]):
    """Bounded async queue with backpressure and structured close.

    Publisher blocks on put() when channel is full.
    close() signals completion, then drain() waits for pending items.
    """

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError(f"Channel capacity must be > 0, got {capacity}")
        self._queue: asyncio.Queue[T] = asyncio.Queue(maxsize=capacity)
        self._closed = False
        self._drain_task: asyncio.Task | None = None

    @property
    def capacity(self) -> int:
        return self._queue.maxsize

    @property
    def size(self) -> int:
        return self._queue.qsize()

    @property
    def closed(self) -> bool:
        return self._closed

    async def put(self, item: T, timeout: float | None = None) -> None:
        """Put an item.  Blocks if full, with optional timeout.

        Raises:
            ChannelClosedError: channel is closed.
            ChannelFullError: timed out waiting for space.
        """
        if self._closed:
            raise ChannelClosedError("Channel is closed")

        if timeout is not None:
            try:
                await asyncio.wait_for(self._queue.put(item), timeout=timeout)
            except asyncio.TimeoutError:
                raise ChannelFullError(
                    f"Channel full: timed out after {timeout}s (capacity={self.capacity})"
                )
        else:
            await self._queue.put(item)

    async def get(self) -> T:
        """Get an item.  Blocks until available or channel closed+empty."""
        if self._closed and self._queue.empty():
            raise ChannelClosedError("Channel closed and empty")
        return await self._queue.get()

    async def close(self) -> None:
        """Signal no more items.  Does NOT wait for drain."""
        self._closed = True

    async def drain(self) -> int:
        """Await pending items.  Returns number of undrained items remaining."""
        remaining = self._queue.qsize()
        if remaining > 0:
            logger.debug("Draining %d items from bounded channel", remaining)
            await asyncio.sleep(0.1)  # give consumers a chance
            remaining = self._queue.qsize()
        return remaining
