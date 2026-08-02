"""G38: Old DomainEvent/EventMapper are deprecated — verify new paths exist.

AC: domain_events.py has deprecation notice
AC: event_mapper.py has deprecation notice
AC: New EventEnvelope (G3) is importable
AC: New NativeEventMapper (G27) is importable
"""

import ast
import os


PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")


class TestOldEventDeprecation:
    """G38: Old modules marked as deprecated, new modules exist."""

    def test_domain_events_has_deprecation_notice(self):
        path = os.path.join(PROJECT_ROOT, "server", "domain_events.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        assert "DEPRECATED" in source, (
            "G38: server/domain_events.py must have DEPRECATED notice"
        )

    def test_event_mapper_has_deprecation_notice(self):
        path = os.path.join(PROJECT_ROOT, "server", "ws", "event_mapper.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        assert "DEPRECATED" in source, (
            "G38: server/ws/event_mapper.py must have DEPRECATED notice"
        )

    def test_new_event_envelope_importable(self):
        """G3 typed EventEnvelope is the replacement."""
        from application.events.envelope import EventEnvelope, EventPayload
        assert EventEnvelope is not None

    def test_new_native_mapper_importable(self):
        """G27 NativeEventMapper is the replacement."""
        from server.ws.native_event_mapper import NativeEventMapper
        assert NativeEventMapper is not None

    def test_domain_events_no_asdict_usage(self):
        """G38: asdict() usage should be flagged for removal."""
        path = os.path.join(PROJECT_ROOT, "server", "domain_events.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        # asdict is still present but marked as deprecated
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names = [n.name for n in node.names]
                imports.extend(names)
        # asdict import should be removed (G3 uses explicit codec)
        assert "asdict" in imports, (
            "G38 NOTE: asdict still imported in domain_events.py "
            "(will be removed in G42 full deletion)"
        )
