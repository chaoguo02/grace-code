"""P16: WS Gateway — acceptance tests."""

import pytest
from listeners.ws_gateway import WsGateway


class TestWsGateway:

    def test_broadcast_to_subscriber(self):
        gw = WsGateway()
        received = []
        gw.subscribe("s1", lambda m: received.append(m))
        gw.broadcast("s1", {"type": "status"})
        assert len(received) == 1

    def test_broadcast_no_subscribers_no_error(self):
        gw = WsGateway()
        gw.broadcast("nonexistent", {"type": "status"})

    def test_subscriber_exception_isolated(self):
        gw = WsGateway()
        good = []
        def bad(_m): raise RuntimeError("boom")
        def ok(m): good.append(m)
        gw.subscribe("s1", bad)
        gw.subscribe("s1", ok)
        gw.broadcast("s1", {"type": "test"})
        assert len(good) == 1

    def test_unsubscribe(self):
        gw = WsGateway()
        received = []
        cb = lambda m: received.append(m)
        gw.subscribe("s1", cb)
        gw.unsubscribe("s1", cb)
        gw.broadcast("s1", {"type": "test"})
        assert len(received) == 0

    def test_different_sessions_isolated(self):
        gw = WsGateway()
        a, b = [], []
        gw.subscribe("s-a", lambda m: a.append(m))
        gw.subscribe("s-b", lambda m: b.append(m))
        gw.broadcast("s-a", {"type": "a"})
        assert len(a) == 1
        assert len(b) == 0
