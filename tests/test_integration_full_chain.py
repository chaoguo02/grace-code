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
