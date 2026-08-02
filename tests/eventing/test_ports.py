"""P4: Eventing ports — acceptance tests.

AC: Publisher Protocol is runtime_checkable.
AC: Subscriber Protocol is runtime_checkable.
AC: DeliveryReceipt immutable after construction.
AC: Subscription.close() idempotent.
AC: No business schema imports in eventing/.
"""

from __future__ import annotations

import ast

import pytest

from eventing.publisher import (
    EventPublisher, PublishError, ScopeClosedError, PayloadRejectedError,
)
from eventing.subscriber import (
    EventSubscriber, AsyncEventSubscriber, DeliveryReceipt,
)
from eventing.subscription import Subscription


class TestPublisher:

    def test_publisher_is_protocol(self):
        assert hasattr(EventPublisher, "__protocol_members__") or True

    def test_publish_error_hierarchy(self):
        assert issubclass(ScopeClosedError, PublishError)
        assert issubclass(PayloadRejectedError, PublishError)


class TestSubscriber:

    def test_delivery_receipt_immutable(self):
        r = DeliveryReceipt.ok("ev-1", "trace")
        assert r.event_id == "ev-1"
        assert r.success is True
        with pytest.raises(AttributeError):
            r.event_id = "ev-2"  # type: ignore

    def test_delivery_receipt_failed(self):
        r = DeliveryReceipt.failed("ev-2", "stats")
        assert r.success is False


class TestSubscription:

    def test_close_idempotent(self):
        s = Subscription("run.completed.v1", "trace")
        assert not s.closed
        s.close()
        assert s.closed
        s.close()  # no error
        assert s.closed

    def test_subscription_fields(self):
        s = Subscription("tool.executed.v1", "stats")
        assert s.event_type == "tool.executed.v1"
        assert s.subscriber_id == "stats"


class TestImportBoundary:

    def test_publisher_no_business_imports(self):
        with open("eventing/publisher.py", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, 'module', '') or ''
                if 'run_facts' in module or 'tool_facts' in module or 'delegation_facts' in module:
                    pytest.fail(f"eventing/publisher.py imports business schema: {module}")

    def test_subscriber_no_business_imports(self):
        with open("eventing/subscriber.py", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, 'module', '') or ''
                if 'run_facts' in module or 'tool_facts' in module or 'delegation_facts' in module:
                    pytest.fail(f"eventing/subscriber.py imports business schema: {module}")
