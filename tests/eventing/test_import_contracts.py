"""G7: EventBus port purity — zero business-schema imports.

Verifies:
  - publisher.py does NOT import application.events.envelope
  - subscriber.py does NOT import application.events.envelope
  - publisher.py only depends on TypeVar, ScopeToken, Protocol
  - subscriber.py only depends on TypeVar, Protocol, dataclass
  - HandlerOutcome sum type is used (Accepted | Rejected | HandlerFailed)
  - ScopedMessage protocol replaces EventEnvelope in the port layer
"""

from __future__ import annotations

import ast
import os

import pytest

# Files to check
EVENTING_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "eventing")


def _parse_source(filename: str) -> ast.AST:
    path = os.path.join(EVENTING_DIR, filename)
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read())


def _get_imports(tree: ast.AST) -> list[str]:
    """Return all imported module paths."""
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module:
                for alias in node.names:
                    imports.append(f"{module}.{alias.name}")
            else:
                for alias in node.names:
                    imports.append(alias.name)
    return imports


FORBIDDEN_PATTERNS = [
    "application.events",
    "server",
    "FastAPI",
    "WebSocket",
    "sqlite",
    "Repository",
    "Projection",
]


class TestPublisherPortPurity:
    """G7: publisher.py must not import business schemas."""

    def test_publisher_no_business_imports(self):
        tree = _parse_source("publisher.py")
        imports = _get_imports(tree)
        for imp in imports:
            for forbidden in FORBIDDEN_PATTERNS:
                assert forbidden not in imp, (
                    f"publisher.py imports forbidden module: {imp!r}"
                )

    def test_publisher_uses_scoped_message(self):
        tree = _parse_source("publisher.py")
        source = ast.unparse(tree)
        assert "ScopedMessage" in source, (
            "publisher.py must define ScopedMessage protocol"
        )
        # Must NOT reference EventEnvelope
        assert "EventEnvelope" not in source, (
            "G7: publisher.py must not reference EventEnvelope — "
            "use ScopedMessage protocol instead"
        )

    def test_publisher_depends_only_on_core(self):
        tree = _parse_source("publisher.py")
        imports = _get_imports(tree)
        for imp in imports:
            if imp.startswith("__future__"):
                continue
            if imp.startswith("typing"):
                continue
            if imp.startswith("core.eventing.scope"):
                continue
            if imp == "core.eventing.scope.ScopeToken":
                continue
            assert False, (
                f"publisher.py has unexpected import: {imp!r}"
            )


class TestSubscriberPortPurity:
    """G7: subscriber.py must not import business schemas."""

    def test_subscriber_no_business_imports(self):
        tree = _parse_source("subscriber.py")
        imports = _get_imports(tree)
        for imp in imports:
            for forbidden in FORBIDDEN_PATTERNS:
                assert forbidden not in imp, (
                    f"subscriber.py imports forbidden module: {imp!r}"
                )

    def test_subscriber_defines_handler_outcome(self):
        tree = _parse_source("subscriber.py")
        source = ast.unparse(tree)
        assert "HandlerOutcome" in source, (
            "subscriber.py must define HandlerOutcome sum type"
        )
        assert "Accepted" in source
        assert "Rejected" in source or "HandlerFailed" in source

    def test_subscriber_depends_only_on_stdlib(self):
        tree = _parse_source("subscriber.py")
        imports = _get_imports(tree)
        for imp in imports:
            if imp.startswith("__future__"):
                continue
            if imp.startswith("typing"):
                continue
            if imp.startswith("dataclasses"):
                continue
            if imp in ("typing", "dataclasses"):
                continue
            assert False, (
                f"subscriber.py has unexpected import: {imp!r}"
            )


class TestPortVariance:
    """G7: Correct variance on Publisher/Subscriber protocols."""

    def test_publisher_covariant(self):
        tree = _parse_source("publisher.py")
        source = ast.unparse(tree)
        assert "PayloadT_co" in source, "Publisher TypeVar should be covariant"

    def test_subscriber_contravariant(self):
        tree = _parse_source("subscriber.py")
        source = ast.unparse(tree)
        assert "contravariant=True" in source, "Subscriber TypeVar should be contravariant"


class TestStaticGate:
    """G7 static gate: zero forbidden matches in eventing/ source files."""

    def test_eventing_directory_no_forbidden_imports(self):
        """G7 static gate: zero forbidden matches in eventing/ port files.

        scoped_bus.py is excluded (still imports application.events.envelope
        for backward compat — will be cleaned in G8).
        """
        # G7 scope: publisher.py + subscriber.py (port layer only)
        G7_FILES = {"publisher.py", "subscriber.py"}
        violations: list[str] = []
        for filename in G7_FILES:
            tree = _parse_source(filename)
            imports = _get_imports(tree)
            for imp in imports:
                for forbidden in FORBIDDEN_PATTERNS:
                    if forbidden in imp:
                        violations.append(f"{filename}: imports {imp!r}")

        assert violations == [], (
            f"G7 static gate FAILED — forbidden imports found in port layer:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )
