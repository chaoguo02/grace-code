"""G31: Native Run E2E — submit → execute → terminal via coordinator.

AC: submit_run_turn uses RunCoordinator (no legacy SQLite path)
AC: No GRACE_RUNTIME_MODE branching
AC: No nested _StorageUoW / _StorageTx
AC: Idempotency conflict detected
AC: Active run check prevents duplicates
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from core.eventing.identifiers import SessionId, RunId
from runtime_core.execution import RuntimeExecution, ConversationSnapshot
from runtime_core.model_actions import TokenUsage
from application.commands.run_commands import SubmitRun, IdempotencyConflict, RunAlreadyActive, ExecuteRun
from application.coordinators.run_coordinator import RunCoordinator
from application.events.schema_registry import SchemaRegistry
from infrastructure.outbox.sqlite_store import SqliteOutboxStore
from infrastructure.sqlite.run_uow import SqliteUnitOfWork
from infrastructure.outbox.owner_lease import OwnerLease
from runtime_core.outcome import RunStatus


class FakeRuntime:
    def run(self, ctx): return object()


@pytest.fixture
def temp_db():
    d = tempfile.mkdtemp()
    db = str(Path(d) / "test.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, run_generation INTEGER DEFAULT 0);
        CREATE TABLE runs (id TEXT PRIMARY KEY, session_id TEXT, turn_id TEXT,
            turn_index INTEGER, idempotency_key TEXT, prompt TEXT,
            status TEXT DEFAULT 'queued', aggregate_version INTEGER DEFAULT 1,
            created_at TEXT, updated_at TEXT);
        CREATE TABLE session_messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, role TEXT, content TEXT, turn_id TEXT, created_at TEXT);
    """)
    SqliteOutboxStore.install(conn)
    conn.execute("INSERT INTO sessions (id) VALUES ('s-e2e')")
    conn.commit()
    conn.close()
    yield db
    import shutil
    shutil.rmtree(d, ignore_errors=True)


class TestNativeRunE2E:
    """G31: End-to-end run submission via native coordinator."""

    def test_submit_via_coordinator(self, temp_db):
        reg = SchemaRegistry()
        outbox = SqliteOutboxStore(temp_db, reg)
        uow = SqliteUnitOfWork(temp_db, outbox)
        coord = RunCoordinator(FakeRuntime(), uow)

        cmd = SubmitRun(session_id=SessionId("s-e2e"), prompt="hello",
                        idempotency_key="ik-e2e")
        result = coord.submit(cmd)
        assert isinstance(result, RunId), f"Expected RunId, got {type(result).__name__}"

        # Verify DB state
        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        run = conn.execute("SELECT * FROM runs WHERE id=?", (str(result),)).fetchone()
        conn.close()
        assert run is not None
        assert run["prompt"] == "hello"

    def test_idempotency_conflict(self, temp_db):
        reg = SchemaRegistry()
        outbox = SqliteOutboxStore(temp_db, reg)
        uow = SqliteUnitOfWork(temp_db, outbox)
        coord = RunCoordinator(FakeRuntime(), uow)

        cmd1 = SubmitRun(session_id=SessionId("s-e2e"), prompt="a",
                         idempotency_key="ik-dup")
        r1 = coord.submit(cmd1)
        assert isinstance(r1, RunId)

        cmd2 = SubmitRun(session_id=SessionId("s-e2e"), prompt="b",
                         idempotency_key="ik-dup")
        r2 = coord.submit(cmd2)
        assert isinstance(r2, IdempotencyConflict)

    def test_no_grace_runtime_mode_in_source(self):
        """G31: run_submission.py must not reference GRACE_RUNTIME_MODE."""
        import ast, os
        path = os.path.join(os.path.dirname(__file__), "..", "..",
                            "server", "services", "run_submission.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        assert "GRACE_RUNTIME_MODE" not in source, (
            "G31: run_submission.py must not reference GRACE_RUNTIME_MODE"
        )
        assert "_StorageUoW" not in source, (
            "G31: _StorageUoW nested class must be removed"
        )
        assert "_StorageTx" not in source, (
            "G31: _StorageTx nested class must be removed"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# H8 — End-to-end fake-adapter verification
# ═══════════════════════════════════════════════════════════════════════════════

class TestE2EFakeAdapter:
    """H8: Full pipeline with fake adapters — submit → execute → terminal."""

    def test_full_pipeline_evidence_and_tokens(self, temp_db):
        """E2E: assemble → submit → execute → verify evidence + tokens."""
        from composition.runtime_composition import assemble
        comp = assemble(temp_db)

        # Submit
        cmd = SubmitRun(session_id=SessionId("s-e2e"), prompt="hello",
                        idempotency_key="ik-e2e-h8")
        result = comp.run_coordinator.submit(cmd)
        assert isinstance(result, RunId), f"Expected RunId, got {type(result).__name__}"
        run_id = result

        # Execute via coordinator
        outcome = comp.runtime.run(
            RuntimeExecution(
                session_id=SessionId("s-e2e"),
                run_id=run_id,
                max_steps=5,
                conversation=ConversationSnapshot(
                    messages=({"role": "user", "content": "hello"},),
                ),
            )
        )
        # H8: After H0-H7, fake LLM returns text + usage, tools return output
        assert outcome.status in (RunStatus.COMPLETED, RunStatus.BLOCKED), (
            f"Expected COMPLETED or BLOCKED, got {outcome.status}"
        )
        # H4: Evidence is None for text-only completions (no tools ran)
        # Tool evidence is verified in test_e2e_with_tool_call below
        # H3: Tokens must be non-zero
        assert outcome.tokens_used > 0, (
            f"H8 FAIL: tokens_used must be > 0, got {outcome.tokens_used}"
        )
        # H3: Input/output separated
        assert outcome.input_tokens > 0, (
            f"H8 FAIL: input_tokens must be > 0, got {outcome.input_tokens}"
        )
        assert outcome.output_tokens > 0, (
            f"H8 FAIL: output_tokens must be > 0, got {outcome.output_tokens}"
        )

    def test_e2e_with_tool_call(self, temp_db):
        """E2E: run with a tool call produces tool evidence."""
        from composition.runtime_composition import assemble
        from runtime_core.model_actions import ToolCall
        from core.json_values import freeze_json

        comp = assemble(temp_db)

        # Create a FakeLLM that returns a tool call
        tc = ToolCall(id="t1", name="read", params=freeze_json({"f": "x"}),
                      usage=TokenUsage(input_tokens=30, output_tokens=10))
        # Override the LLM port with a controlled one
        comp.runtime_ports.llm.invoke = lambda m, t=None: tc

        outcome = comp.runtime.run(
            RuntimeExecution(
                session_id=SessionId("s-e2e-tool"),
                run_id=RunId("r-e2e-tool"),
                max_steps=5,
                conversation=ConversationSnapshot(
                    messages=({"role": "user", "content": "read x"},),
                ),
            )
        )
        # H4: Tool evidence must include the tool call
        assert outcome.evidence is not None
        assert len(outcome.evidence.tool_calls) >= 1, (
            f"H8: expected tool evidence, got {len(outcome.evidence.tool_calls)}"
        )

    def test_e2e_performance_baseline(self, temp_db):
        """H8: Fake adapter E2E must complete in < 2 seconds."""
        import time as _time
        from composition.runtime_composition import assemble
        comp = assemble(temp_db)

        started = _time.monotonic()
        outcome = comp.runtime.run(
            RuntimeExecution(
                session_id=SessionId("s-perf"),
                run_id=RunId("r-perf"),
                max_steps=3,
                conversation=ConversationSnapshot(
                    messages=({"role": "user", "content": "hi"},),
                ),
            )
        )
        elapsed = (_time.monotonic() - started) * 1000
        assert elapsed < 2000, (
            f"H8: E2E too slow: {elapsed:.0f}ms (must be < 2000ms)"
        )
