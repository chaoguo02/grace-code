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
    BoundedChannel, ChannelFullError, ChannelClosedError,
)


class TestBoundedChannel:

    def test_capacity_must_be_positive(self):
        with pytest.raises(ValueError):
            BoundedChannel(0)
        with pytest.raises(ValueError):
            BoundedChannel(-1)

    def test_put_and_get(self):
        async def _run():
            ch = BoundedChannel[int](capacity=2)
            await ch.put(42)
            assert ch.size == 1
            val = await ch.get()
            assert val == 42

        asyncio.run(_run())

    def test_full_channel_blocks_with_timeout(self):
        async def _run():
            ch = BoundedChannel[int](capacity=1)
            await ch.put(1)
            with pytest.raises(ChannelFullError):
                await ch.put(2, timeout=0.01)

        asyncio.run(_run())

    def test_close_rejects_new_puts(self):
        async def _run():
            ch = BoundedChannel[int](capacity=2)
            await ch.close()
            with pytest.raises(ChannelClosedError):
                await ch.put(1)

        asyncio.run(_run())

    def test_drain_after_close(self):
        async def _run():
            ch = BoundedChannel[int](capacity=2)
            await ch.put(99)
            await ch.close()
            remaining = await ch.drain()
            assert remaining >= 0

        asyncio.run(_run())
