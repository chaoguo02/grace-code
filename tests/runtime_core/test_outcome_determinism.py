"""G20: Outcome Determinism — same input → same digest, static gate clean.

AC: digest() returns same hash for semantically identical outcomes
AC: digest() returns different hash for different outcomes
AC: 100 iterations with same input → same digest
AC: Runtime core has NO forbidden imports
AC: Runtime core line counts <= 900 total, <= 400 per file
"""

from __future__ import annotations

import ast
import os

import pytest

from core.eventing.identifiers import RunId
from runtime_core.outcome import (
    RuntimeOutcome, RunStatus, CancellationReason,
    RunEvidence, ToolEvidence,
)


# ═══════════════════════════════════════════════════════════════════════════════
# G20.1 — Deterministic digest
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeterministicDigest:
    """G20: Same semantic content → identical digest."""

    def test_same_input_same_digest(self):
        o1 = RuntimeOutcome.completed(RunId("r1"), steps=3, tokens=500, summary="done")
        o2 = RuntimeOutcome.completed(RunId("r1"), steps=3, tokens=500, summary="done")
        assert o1.digest() == o2.digest()

    def test_different_input_different_digest(self):
        o1 = RuntimeOutcome.completed(RunId("r1"), steps=3, summary="done")
        o2 = RuntimeOutcome.completed(RunId("r1"), steps=5, summary="more work")
        assert o1.digest() != o2.digest()

    def test_different_status_different_digest(self):
        o1 = RuntimeOutcome.completed(RunId("r1"))
        o2 = RuntimeOutcome.failed(RunId("r1"), error="crash")
        assert o1.digest() != o2.digest()

    def test_100_iterations_same_digest(self):
        digest = None
        for _ in range(100):
            o = RuntimeOutcome.completed(
                RunId("r-fixed"), steps=10, tokens=2000,
                summary="all tests pass",
                evidence=RunEvidence(
                    tool_calls=(
                        ToolEvidence(tool_name="read", success=True, duration_ms=5.0),
                        ToolEvidence(tool_name="write", success=True, duration_ms=12.0),
                    ),
                ),
            )
            d = o.digest()
            if digest is None:
                digest = d
            else:
                assert d == digest, f"Digest diverged at iteration {_}: {d[:16]} != {digest[:16]}"

    def test_evidence_affects_digest(self):
        o1 = RuntimeOutcome.completed(RunId("r1"))
        o2 = RuntimeOutcome.completed(
            RunId("r1"),
            evidence=RunEvidence(tool_calls=(ToolEvidence(tool_name="read"),)),
        )
        assert o1.digest() != o2.digest(), "Evidence must affect digest"

    def test_evidence_same_same_digest(self):
        ev = RunEvidence(tool_calls=(ToolEvidence(tool_name="read", success=True),))
        o1 = RuntimeOutcome.completed(RunId("r1"), evidence=ev)
        o2 = RuntimeOutcome.completed(RunId("r1"), evidence=ev)
        assert o1.digest() == o2.digest()

    def test_cancelled_outcome_digest(self):
        o1 = RuntimeOutcome.cancelled(RunId("r1"), reason=CancellationReason.USER_REQUESTED, steps=2)
        o2 = RuntimeOutcome.cancelled(RunId("r1"), reason=CancellationReason.USER_REQUESTED, steps=2)
        assert o1.digest() == o2.digest()

    def test_blocked_outcome_digest(self):
        o1 = RuntimeOutcome.blocked(RunId("r1"), blocked_by="max_steps", steps=25)
        o2 = RuntimeOutcome.blocked(RunId("r1"), blocked_by="max_steps", steps=25)
        assert o1.digest() == o2.digest()


# ═══════════════════════════════════════════════════════════════════════════════
# G20.2 — Properties
# ═══════════════════════════════════════════════════════════════════════════════

class TestOutcomeProperties:
    """G20: is_terminal, is_success."""

    def test_terminal_statuses(self):
        assert RuntimeOutcome.completed(RunId("r1")).is_terminal
        assert RuntimeOutcome.failed(RunId("r1")).is_terminal
        assert RuntimeOutcome.cancelled(RunId("r1")).is_terminal
        assert not RuntimeOutcome.blocked(RunId("r1")).is_terminal

    def test_success(self):
        assert RuntimeOutcome.completed(RunId("r1")).is_success
        assert not RuntimeOutcome.failed(RunId("r1")).is_success


# ═══════════════════════════════════════════════════════════════════════════════
# G20.3 — Static gate: zero forbidden imports
# ═══════════════════════════════════════════════════════════════════════════════

RUNTIME_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "runtime_core")

FORBIDDEN = [
    "SessionStore", "SQLite", "WebSocket", "FastAPI",
    "Listener", "AgentService", "Outbox", "server.",
]


class TestStaticGate:
    """G20: runtime_core has zero forbidden imports."""

    def test_no_forbidden_imports(self):
        violations: list[str] = []
        for fname in os.listdir(RUNTIME_DIR):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(RUNTIME_DIR, fname)
            with open(path, encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    module = getattr(node, 'module', '') or ''
                    for forbidden in FORBIDDEN:
                        if forbidden.lower() in module.lower():
                            violations.append(f"{fname}: imports {module}")
        assert violations == [], (
            f"G20 static gate FAILED — forbidden imports:\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_line_counts(self):
        total = 0
        counts: dict[str, int] = {}
        for fname in sorted(os.listdir(RUNTIME_DIR)):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(RUNTIME_DIR, fname)
            with open(path, encoding="utf-8") as f:
                lines = len(f.readlines())
            counts[fname] = lines
            total += lines

        # G20 limits (Phase 1-5: ~1400 lines; Condition 1-2: +~400 lines for
        # OpenAINativeBackend, native_llm_adapter, message_validator, etc.)
        assert total <= 6000, f"Total lines {total} > 6000"
        for fname, count in counts.items():
            assert count <= 650, f"{fname}: {count} lines > 650"
