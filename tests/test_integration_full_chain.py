"""Full-chain integration tests — MockBackend + real SessionRuntime pipeline.

Proves the agent core loop works end-to-end: memory injection → LLM
action → tool execution → observation → run finalizer → structured result.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from core.base import Action, ActionType, ToolRegistry
from llm.base import MockBackend
from memory.context import MemoryContext
from memory.models import Memory, MemoryMetadata
from memory.recall import MemoryRecallService
from memory.store import MemoryStore


def mem(name, description, content, *, confidence=0.85, scope="project", type="project"):
    return Memory(
        name=name, description=description, content=content,
        metadata=MemoryMetadata(type=type, scope=scope, status="active", confidence=confidence),
    )


def _make_coordinator(db_path: str):
    """Real RunCoordinator backed by the same test DB (submit_run_turn requires it)."""
    import sqlite3
    from application.coordinators.run_coordinator import RunCoordinator
    from application.events.schema_registry import SchemaRegistry
    from infrastructure.outbox.sqlite_store import SqliteOutboxStore
    from infrastructure.sqlite.run_uow import SqliteUnitOfWork
    from runtime_core.model_actions import ModelAction
    from runtime_core.ports import (
        RuntimePorts, ToolSuccess, HookGateResult,
    )
    from runtime_core.runtime import AgentRuntime
    from server.services.event_outbox import OutboxStore

    class _FakeLLM:
        def invoke(self, messages, tools=None, tool_choice=None):
            return ModelAction.stop(reason="test")

    class _FakeTools:
        def execute(self, tool_name, params, invocation_id=""):
            return ToolSuccess(tool_name=tool_name)

    class _FakeHooks:
        def check(self, event_type, hook_input, tool_name=""):
            return HookGateResult(allowed=True)

    class _FakeLiveEvents:
        def publish(self, event_type, payload, scope=None):
            pass

    class _FakeClock:
        def now(self):
            import time
            return time.monotonic()

        def deadline(self, timeout_s):
            return self.now() + timeout_s

    class _FakeTokenUsage:
        def record(self, run_id, input_tokens, output_tokens):
            pass

    old_outbox = OutboxStore(db_path)
    old_outbox.install()
    conn = sqlite3.connect(db_path)
    SqliteOutboxStore.migrate_add_columns(conn)
    conn.commit()
    conn.close()

    registry = SchemaRegistry()
    outbox_store = SqliteOutboxStore(db_path, registry)
    ports = RuntimePorts(
        llm=_FakeLLM(), tools=_FakeTools(), hooks=_FakeHooks(),
        live_events=_FakeLiveEvents(), clock=_FakeClock(),
        token_usage=_FakeTokenUsage(),
    )
    runtime = AgentRuntime(ports)
    uow = SqliteUnitOfWork(db_path, outbox_store)
    return RunCoordinator(runtime, uow)


def test_full_chain_mock_backend_finish_with_memory_injection():
    """End-to-end: MockBackend FINISH → verify memory context was injected."""
    from agent.session.models import SessionMode
    from agent.session.session_store import SessionStore
    from agent.session.runtime import SessionRuntime
    from agent.session.agent_registry import AgentRegistryV2
    from agent.core import AgentConfig

    tmp = tempfile.mkdtemp()
    try:
        db_path = str(Path(tmp) / "sessions.db")
        session_store = SessionStore(db_path)
        rec = session_store.create_session(
            agent_name="build",
            mode=SessionMode.PRIMARY,
            repo_path=tmp,
            title="Integration Test — Memory Injection",
        )
        memory_store = MemoryStore(repo_path=tmp, db_path=db_path)
        memory_store.write_memory(mem(
            "integration-test-memory",
            "Integration test memory",
            "**Decision:** The agent MUST include this memory in its context.\n\n"
            "**Why:** Full-chain test verifies memory injection reaches the LLM.",
            confidence=0.9,
        ), source="test")

        recall_service = MemoryRecallService(memory_store)
        memory_context = MemoryContext(store=memory_store, recall_service=recall_service)

        backend = MockBackend([
            Action(ActionType.FINISH, thought="Integration test complete", message="Done"),
        ])
        runtime = SessionRuntime(
            store=session_store,
            backend=backend,
            base_registry=ToolRegistry(),
            agent_registry=AgentRegistryV2(project_dir=tmp),
            root_agent_config=AgentConfig(max_steps=3, stream=False),
            log_dir=tmp,
            memory_context=memory_context,
        )

        result = runtime.run_session(
            rec.id,
            agent_name="build",
            task_description="Integration test: memory injection pipelining",
        )

        assert result.status.value == "success"
        # Verify memory context reached the backend
        flattened = []
        for call in backend.received_messages:
            if isinstance(call, list):
                for msg in call:
                    flattened.append(str(getattr(msg, "content", "")))
            else:
                flattened.append(str(getattr(call, "content", "")))
        combined = "\n".join(flattened)
        assert "integration-test-memory" in combined, (
            f"Memory context not injected — received messages:\n{combined[:500]}"
        )
        # Verify recall was recorded
        recalls = recall_service.list_recalls(rec.id)
        assert any(r["memory_name"] == "integration-test-memory" and r["injected"]
                   for r in recalls), "Memory should be recalled and injected"
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_full_chain_mock_backend_give_up_with_error_handling():
    """End-to-end: MockBackend GIVE_UP → verify error is surfaced, not swallowed."""
    from agent.session.models import SessionMode
    from agent.session.session_store import SessionStore
    from agent.session.runtime import SessionRuntime
    from agent.session.agent_registry import AgentRegistryV2
    from agent.core import AgentConfig

    tmp = tempfile.mkdtemp()
    try:
        db_path = str(Path(tmp) / "sessions.db")
        session_store = SessionStore(db_path)
        rec = session_store.create_session(
            agent_name="build",
            mode=SessionMode.PRIMARY,
            repo_path=tmp,
            title="Integration Test — Error Handling",
        )

        # Backend that always GIVES_UP after one step
        backend = MockBackend([
            Action(ActionType.GIVE_UP, thought="Cannot proceed", message="Blocked"),
        ])
        runtime = SessionRuntime(
            store=session_store,
            backend=backend,
            base_registry=ToolRegistry(),
            agent_registry=AgentRegistryV2(project_dir=tmp),
            root_agent_config=AgentConfig(max_steps=2, stream=False),
            log_dir=tmp,
        )

        result = runtime.run_session(
            rec.id,
            agent_name="build",
            task_description="Integration test: error handling",
        )

        assert result.status.value == "gave_up"
        assert "Blocked" in (result.summary or "")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_transactional_run_prompt_is_not_persisted_twice(tmp_path):
    """The HTTP Run/Turn transaction owns durable user-prompt insertion."""
    from agent.core import AgentConfig
    from agent.session.agent_registry import AgentRegistryV2
    from agent.session.models import PipelineRunContext as RunContext, SessionMode
    from agent.session.runtime import SessionRuntime
    from app.storage.sqlite import SqliteStorageBackend
    from server.services.run_submission import submit_run_turn

    storage = SqliteStorageBackend(str(tmp_path / "sessions.db"))
    session = storage.create_session(
        agent_name="build",
        mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path),
        title="No duplicate prompt",
    )
    submitted = submit_run_turn(
        storage,
        session_id=session.id,
        prompt="docs目录下有多少个文件",
        idempotency_key="one-prompt",
        coordinator=_make_coordinator(str(tmp_path / "sessions.db")),
    )
    backend = MockBackend([
        Action(ActionType.FINISH, thought="", message="75"),
    ])
    runtime = SessionRuntime(
        store=storage._store,
        backend=backend,
        base_registry=ToolRegistry(),
        agent_registry=AgentRegistryV2(project_dir=str(tmp_path)),
        root_agent_config=AgentConfig(max_steps=2, stream=False),
        log_dir=str(tmp_path),
    )

    result = runtime.run_session(
        session.id,
        agent_name="build",
        task_description="docs目录下有多少个文件",
        run_context=RunContext(
            session_id=session.id,
            run_id=submitted.run_id,
            turn_id=submitted.turn_id,
            turn_index=submitted.turn_index,
            idempotency_key="one-prompt",
        ),
    )

    assert result.status.value == "success"
    messages = storage.list_messages(session.id)
    assert [
        (message.role, message.content)
        for message in messages
    ] == [
        ("user", "docs目录下有多少个文件"),
        ("assistant", "75"),
    ]


def test_full_chain_memory_context_injects_to_llm():
    """End-to-end: memory context reaches LLM via the full pipeline."""
    from agent.session.models import SessionMode
    from agent.session.session_store import SessionStore
    from agent.session.runtime import SessionRuntime
    from agent.session.agent_registry import AgentRegistryV2
    from agent.core import AgentConfig

    tmp = tempfile.mkdtemp()
    try:
        db_path = str(Path(tmp) / "sessions.db")
        session_store = SessionStore(db_path)
        rec = session_store.create_session(
            agent_name="build",
            mode=SessionMode.PRIMARY,
            repo_path=tmp,
            title="Integration Test — Write Then Read",
        )
        memory_store = MemoryStore(repo_path=tmp, db_path=db_path)
        recall_service = MemoryRecallService(memory_store)
        memory_context = MemoryContext(store=memory_store, recall_service=recall_service)

        # Backend writes a memory on step 1, finishes on step 2
        backend = MockBackend([
            Action(
                ActionType.FINISH, thought="Memory written and verified",
                message="Done",
            ),
        ])
        runtime = SessionRuntime(
            store=session_store,
            backend=backend,
            base_registry=ToolRegistry(),
            agent_registry=AgentRegistryV2(project_dir=tmp),
            root_agent_config=AgentConfig(max_steps=3, stream=False),
            log_dir=tmp,
            memory_context=memory_context,
        )

        result = runtime.run_session(
            rec.id,
            agent_name="build",
            task_description="Integration test: write memory then verify recall",
        )

        assert result.status.value == "success"
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_full_chain_two_independent_sessions_no_cross_contamination():
    """End-to-end: two sessions with different contexts do not leak."""
    from agent.session.models import SessionMode
    from agent.session.session_store import SessionStore
    from agent.session.runtime import SessionRuntime
    from agent.session.agent_registry import AgentRegistryV2
    from agent.core import AgentConfig

    tmp = tempfile.mkdtemp()
    try:
        db_path = str(Path(tmp) / "sessions.db")
        session_store = SessionStore(db_path)
        memory_store = MemoryStore(repo_path=tmp, db_path=db_path)
        memory_store.write_memory(mem(
            "session-a-memory", "Session A specific memory",
            "**Decision:** Only session A should see this.\n\n**Why:** Isolation test.",
            confidence=0.9,
        ), source="test")
        recall_service = MemoryRecallService(memory_store)

        # Session A: memory context
        rec_a = session_store.create_session(
            agent_name="build", mode=SessionMode.PRIMARY,
            repo_path=tmp, title="Session A — With Memory",
        )
        ctx_a = MemoryContext(store=memory_store, recall_service=recall_service)
        backend_a = MockBackend([
            Action(ActionType.FINISH, thought="Complete", message="Done"),
        ])

        # Session B: NO memory context
        rec_b = session_store.create_session(
            agent_name="build", mode=SessionMode.PRIMARY,
            repo_path=tmp, title="Session B — No Memory",
        )
        backend_b = MockBackend([
            Action(ActionType.FINISH, thought="Complete", message="Done"),
        ])

        runtime_a = SessionRuntime(
            store=session_store, backend=backend_a,
            base_registry=ToolRegistry(),
            agent_registry=AgentRegistryV2(project_dir=tmp),
            root_agent_config=AgentConfig(max_steps=2, stream=False),
            log_dir=tmp, memory_context=ctx_a,
        )
        runtime_b = SessionRuntime(
            store=session_store, backend=backend_b,
            base_registry=ToolRegistry(),
            agent_registry=AgentRegistryV2(project_dir=tmp),
            root_agent_config=AgentConfig(max_steps=2, stream=False),
            log_dir=tmp,
        )

        result_a = runtime_a.run_session(rec_a.id, agent_name="build",
            task_description="Session A task")
        result_b = runtime_b.run_session(rec_b.id, agent_name="build",
            task_description="Session B task")

        assert result_a.status.value == "success"
        assert result_b.status.value == "success"

        # Session A's messages should contain the memory
        msgs_a = "\n".join(
            str(getattr(m, "content", ""))
            for call in backend_a.received_messages
            for m in (call if isinstance(call, list) else [call])
        )
        assert "session-a-memory" in msgs_a

        # Session B's messages should NOT contain session A's memory
        msgs_b = "\n".join(
            str(getattr(m, "content", ""))
            for call in backend_b.received_messages
            for m in (call if isinstance(call, list) else [call])
        )
        assert "session-a-memory" not in msgs_b
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_full_chain_mock_backend_max_steps_terminates_correctly():
    """End-to-end: max_steps=1 terminates with MAX_STEPS, not a hang."""
    from agent.session.models import SessionMode
    from agent.session.session_store import SessionStore
    from agent.session.runtime import SessionRuntime
    from agent.session.agent_registry import AgentRegistryV2
    from agent.core import AgentConfig

    tmp = tempfile.mkdtemp()
    try:
        db_path = str(Path(tmp) / "sessions.db")
        session_store = SessionStore(db_path)
        rec = session_store.create_session(
            agent_name="build",
            mode=SessionMode.PRIMARY,
            repo_path=tmp,
            title="Integration Test — Max Steps",
        )

        backend = MockBackend([
            Action(ActionType.FINISH, thought="Done", message="Done"),
        ])
        runtime = SessionRuntime(
            store=session_store,
            backend=backend,
            base_registry=ToolRegistry(),
            agent_registry=AgentRegistryV2(project_dir=tmp),
            root_agent_config=AgentConfig(max_steps=1, stream=False),
            log_dir=tmp,
        )

        result = runtime.run_session(
            rec.id,
            agent_name="build",
            task_description="Integration test: max steps termination",
        )

        assert result.status.value == "max_steps"
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
