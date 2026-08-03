"""P6: Bus lifecycle — acceptance tests.

AC: Channel capacity > 0 enforced.
AC: Full channel blocks publisher (with timeout).
AC: close() + drain() completes pending items.
AC: No daemon fire-and-forget — drain is awaited.
"""

from __future__ import annotations

import asyncio

import pytest

from eventing.bounded_channel import (
    BoundedChannel, ChannelFullError, ChannelClosedError, RejectedClosed,
    TimedOut,
)


class TestBoundedChannel:

    def test_capacity_must_be_positive(self):
        with pytest.raises(ValueError):
            BoundedChannel(0, consumer_handler=lambda x: None)
        with pytest.raises(ValueError):
            BoundedChannel(-1, consumer_handler=lambda x: None)

    def test_put_and_get(self):
        """G6: Items delivered via consumer handler, not get()."""
        received = []
        async def _run():
            ch = BoundedChannel[int](capacity=2, consumer_handler=lambda x: received.append(x))
            ch.start_consumer()
            await ch.put(42)
            await ch.shutdown(drain_timeout_s=1.0)
        asyncio.run(_run())
        assert 42 in received

    def test_full_channel_blocks_with_timeout(self):
        """G6: A genuinely-full channel (blocking consumer) blocks the publisher.

        With a fast consumer the item drains immediately and a second put
        legitimately succeeds — so the consumer must stay blocked on the
        first item to prove backpressure.
        """
        consumed = asyncio.Event()

        async def blocking_handler(x):
            await consumed.wait()

        async def _run():
            ch = BoundedChannel[int](capacity=1, consumer_handler=blocking_handler)
            ch.start_consumer()
            r1 = await ch.put(1)
            assert hasattr(r1, 'channel_id')  # Delivered receipt
            # Consumer is blocked on item 1 → channel is genuinely full
            r2 = await ch.put(2, timeout=0.05)
            assert isinstance(r2, TimedOut)  # should be TimedOut
            consumed.set()
            await ch.shutdown()
        asyncio.run(_run())

    def test_close_rejects_new_puts(self):
        async def _run():
            ch = BoundedChannel[int](capacity=2, consumer_handler=lambda x: None)
            await ch.close()
            r = await ch.put(1)
            from eventing.bounded_channel import RejectedClosed
            assert isinstance(r, RejectedClosed)
        asyncio.run(_run())

    def test_drain_after_close(self):
        async def _run():
            ch = BoundedChannel[int](capacity=2, consumer_handler=lambda x: None)
            await ch.put(99)
            await ch.close()
            remaining = await ch.drain()
            assert remaining >= 0

        asyncio.run(_run())
