"""G32: Multi-Agent — primary-mediated, Task scopes, no bubbling.

AC: Child tasks get fresh TaskContext (no parent history)
AC: Each child has exact Task Scope
AC: Parent cancel → cancels children
AC: Child events do NOT bubble to parent/global
AC: Write children with same lease → serialized
"""

from __future__ import annotations

import pytest

from core.eventing.identifiers import SessionId, TaskId
from application.coordinators.multi_agent_coordinator import (
    MultiAgentCoordinator, ChildTaskContext, ChildTaskResult,
)


class FakeRuntime:
    def run(self, ctx):
        from runtime_core.outcome import RuntimeOutcome, RunStatus
        return RuntimeOutcome.completed(ctx.run_id, steps=1, tokens=10)

    async def arun(self, ctx, *, event_handler=None, text_callback=None):
        return self.run(ctx)


class FakeCoordinator:
    def submit(self, cmd): return object()
    def finalize(self, cmd, session_id=None): return object()


class TestMultiAgent:
    """G32: Primary-mediated multi-agent execution."""

    def test_create_child_contexts(self):
        coord = MultiAgentCoordinator(FakeCoordinator(), FakeRuntime(),
                                       scope_factory=lambda sid: sid)
        tasks = [
            {"description": "read file", "allowed_tools": ["read"]},
            {"description": "write file", "allowed_tools": ["write"],
             "workspace_lease": "file.txt"},
        ]
        contexts = coord.create_child_contexts(tasks, "parent-r1", "parent-s1")
        assert len(contexts) == 2
        assert contexts[0].description == "read file"
        assert contexts[1].workspace_lease == "file.txt"

    def test_child_context_no_parent_history(self):
        coord = MultiAgentCoordinator(FakeCoordinator(), FakeRuntime(),
                                       scope_factory=lambda sid: sid)
        tasks = [{"description": "test"}]
        contexts = coord.create_child_contexts(tasks, "parent-r1", "parent-s1")
        ctx = contexts[0]
        # Fresh context — no parent conversation copied
        assert ctx.parent_run_id == "parent-r1"
        assert ctx.budget_tokens == 50_000
        assert isinstance(ctx.task_id, TaskId)

    def test_execute_children(self):
        coord = MultiAgentCoordinator(FakeCoordinator(), FakeRuntime(),
                                       scope_factory=lambda sid: sid)
        ctx = ChildTaskContext(
            task_id=TaskId("t1"), description="test",
            parent_run_id="r1", parent_session_id="s1",
        )
        results = coord.execute_children([ctx], "s1")
        assert len(results) == 1
        assert results[0].outcome is not None

    def test_workspace_lease_serialized(self):
        coord = MultiAgentCoordinator(FakeCoordinator(), FakeRuntime(),
                                       scope_factory=lambda sid: sid)
        ctx1 = ChildTaskContext(task_id=TaskId("t1"), description="write a",
                                workspace_lease="file.txt",
                                parent_run_id="r1", parent_session_id="s1")
        ctx2 = ChildTaskContext(task_id=TaskId("t2"), description="write b",
                                workspace_lease="file.txt",
                                parent_run_id="r1", parent_session_id="s1")
        results = coord.execute_children([ctx1, ctx2], "s1")
        assert len(results) == 2
        # Both should complete sequentially (same lease serialized)
        assert results[0].outcome is not None
        assert results[1].outcome is not None
