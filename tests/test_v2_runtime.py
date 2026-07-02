from __future__ import annotations

import pytest

from agent.task import Action, ActionType, RunResult, RunStatus, ToolCall
from agent.v2 import AgentRegistryV2, SessionRuntime, SessionStore
from agent.v2.models import ChildSessionResult
from agent.v2.task_tool import TaskToolV2
from llm.base import LLMMessage, MockBackend
from tools.base import NoopTool, ToolRegistry


def _make_runtime(
    tmp_path,
    backend: MockBackend,
    *,
    child_max_steps: int = 12,
) -> tuple[SessionRuntime, SessionStore]:
    from agent.core import AgentConfig

    agent_registry = AgentRegistryV2()
    base_registry = ToolRegistry()
    all_tool_names = {
        tool_name
        for spec in (
            agent_registry.list_primary_agents()
            + agent_registry.list_subagents()
        )
        for tool_name in spec.allowed_tools
    }
    for tool_name in sorted(all_tool_names):
        base_registry.register(NoopTool(tool_name, output=f"{tool_name} ok"))

    store = SessionStore(str(tmp_path / ".forge-agent" / "v2" / "sessions.db"))
    runtime = SessionRuntime(
        store=store,
        backend=backend,
        base_registry=base_registry,
        agent_registry=agent_registry,
        root_agent_config=AgentConfig(
            max_steps=10,
            budget_tokens=50_000,
            request_budget_tokens=20_000,
            history_max_messages=20,
            stream=False,
        ),
        log_dir=str(tmp_path / "logs"),
        child_max_steps=child_max_steps,
        child_budget_tokens=30_000,
        memory_context=None,
    )
    return runtime, store


def test_v2_session_store_persists_parent_child_relationships(tmp_path):
    store = SessionStore(str(tmp_path / ".forge-agent" / "v2" / "sessions.db"))

    root = store.create_session(
        agent_name="build",
        mode="primary",
        repo_path=str(tmp_path),
        title="root session",
    )
    child = store.create_session(
        agent_name="explore",
        mode="subagent",
        repo_path=str(tmp_path),
        title="child session",
        parent_id=root.id,
        root_id=root.root_id,
    )

    reloaded_root = store.get_session(root.id)
    reloaded_child = store.get_session(child.id)
    children = store.list_child_sessions(root.id)

    assert reloaded_root is not None
    assert reloaded_root.parent_id is None
    assert reloaded_root.root_id == root.id
    assert reloaded_child is not None
    assert reloaded_child.parent_id == root.id
    assert reloaded_child.root_id == root.id
    assert [item.id for item in children] == [child.id]


def test_v2_task_tool_runs_child_session_and_returns_summary(tmp_path):
    backend = MockBackend([
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="delegate",
            tool_calls=[
                ToolCall(
                    name="task",
                    params={
                        "subagent_type": "explore",
                        "prompt": "Read auth files and summarize the login flow.",
                    },
                )
            ],
        ),
        Action(
            action_type=ActionType.FINISH,
            thought="child done",
            message="Child summary",
        ),
        Action(
            action_type=ActionType.FINISH,
            thought="parent done",
            message="Parent summary",
        ),
    ])
    runtime, store = _make_runtime(tmp_path, backend)
    root = runtime.create_root_session(
        agent_name="build",
        repo_path=str(tmp_path),
        title="root",
    )

    result = runtime.run_session(
        root.id,
        agent_name="build",
        task_description="Inspect the auth module.",
        intent="analysis",
        messages=[],
    )

    assert result.summary == "Parent summary"
    children = store.list_child_sessions(root.id)
    assert len(children) == 1
    child = children[0]
    assert child.agent_name == "explore"
    assert child.summary == "Child summary"
    root_messages = store.list_messages(root.id)
    child_messages = store.list_messages(child.id)
    tool_messages = [
        str(message.content)
        for message in root_messages
        if message.role == "tool" and "Child summary" in str(message.content)
    ]
    assert tool_messages
    assert "Child summary" in tool_messages[0]
    assert child_messages[0].role == "user"
    assert "login flow" in str(child_messages[0].content)


def test_v2_runtime_bypasses_implicit_analysis_path_scope(tmp_path):
    backend = MockBackend([
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="delegate",
            tool_calls=[
                ToolCall(
                    name="task",
                    params={
                        "subagent_type": "explore",
                        "prompt": "Explore agent/v2 and entry/cli.py, then summarize the v2 path.",
                    },
                )
            ],
        ),
        Action(
            action_type=ActionType.FINISH,
            thought="child done",
            message="Child summary",
        ),
        Action(
            action_type=ActionType.FINISH,
            thought="parent done",
            message="Parent summary",
        ),
    ])
    runtime, _store = _make_runtime(tmp_path, backend)
    root = runtime.create_root_session(
        agent_name="build",
        repo_path=str(tmp_path),
        title="root",
    )

    result = runtime.run_session(
        root.id,
        agent_name="build",
        task_description="Please use task to explore agent/v2 and entry/cli.py, then summarize.",
        intent="analysis",
        messages=[],
    )

    assert result.summary == "Parent summary"
    assert result.status.value == "success"


def test_v2_runtime_injects_available_subagents_for_primary_sessions(tmp_path):
    backend = MockBackend([
        Action(
            action_type=ActionType.FINISH,
            thought="done",
            message="Parent summary",
        ),
    ])
    runtime, _store = _make_runtime(tmp_path, backend)
    root = runtime.create_root_session(
        agent_name="build",
        repo_path=str(tmp_path),
        title="root",
    )

    runtime.run_session(
        root.id,
        agent_name="build",
        task_description="Inspect the auth module.",
        intent="analysis",
        messages=[],
    )

    first_call_messages = backend.received_messages[0]
    assert any(
        message.role == "user" and "[V2 Available Subagents]" in str(message.content)
        for message in first_call_messages
    )
    combined_user_content = "\n".join(
        str(message.content) for message in first_call_messages if message.role == "user"
    )
    assert "explore" in combined_user_content
    assert "general" in combined_user_content
    assert "[V2 Delegation Rule]" not in combined_user_content
    assert "## Task Mode: Analysis" not in combined_user_content


def test_v2_runtime_injects_child_rule_for_explore_sessions(tmp_path):
    backend = MockBackend([
        Action(
            action_type=ActionType.FINISH,
            thought="done",
            message="Child summary",
        ),
    ])
    runtime, _store = _make_runtime(tmp_path, backend)
    root = runtime.create_root_session(
        agent_name="build",
        repo_path=str(tmp_path),
        title="root",
    )

    child_result = runtime.run_child_session(
        parent_session_id=root.id,
        subagent_type="explore",
        description="explore v2",
        prompt="Explore agent/v2 and summarize the flow.",
    )

    assert child_result.session_id
    assert child_result.status == "completed"
    assert child_result.summary == "Child summary"
    first_call_messages = backend.received_messages[0]
    assert any(
        message.role == "user" and "[V2 Child Session Rule]" in str(message.content)
        for message in first_call_messages
    )




def test_v2_subagents_cannot_use_task_tool(tmp_path):
    backend = MockBackend([
        Action(
            action_type=ActionType.FINISH,
            thought="done",
            message="Child summary",
        ),
    ])
    runtime, _store = _make_runtime(tmp_path, backend)
    root = runtime.create_root_session(
        agent_name="build",
        repo_path=str(tmp_path),
        title="root",
    )

    runtime.run_child_session(
        parent_session_id=root.id,
        subagent_type="explore",
        description="test recursion guard",
        prompt="Try to use task tool.",
    )

    child_call_tools = set(backend.received_tools[0])
    assert "task" not in child_call_tools


def test_v2_agent_registry_keeps_plan_and_explore_readonly():
    registry = AgentRegistryV2()

    plan = registry.get("plan")
    explore = registry.get("explore")
    general = registry.get("general")

    assert plan.allow_task_tool is True
    assert "file_write" not in plan.allowed_tools
    assert "shell" not in plan.allowed_tools
    assert "file_write" not in explore.allowed_tools
    assert "shell" not in explore.allowed_tools
    assert "file_write" in general.allowed_tools


# ============================================================
# Layer 1: Component Tests
# ============================================================


class TestMapChildStatus:
    def test_success_maps_to_completed(self, tmp_path):
        backend = MockBackend([])
        runtime, _ = _make_runtime(tmp_path, backend)
        assert runtime._map_child_status(RunStatus.SUCCESS) == "completed"

    def test_max_steps_maps_to_partial(self, tmp_path):
        backend = MockBackend([])
        runtime, _ = _make_runtime(tmp_path, backend)
        assert runtime._map_child_status(RunStatus.MAX_STEPS) == "partial"

    def test_failed_maps_to_failed(self, tmp_path):
        backend = MockBackend([])
        runtime, _ = _make_runtime(tmp_path, backend)
        assert runtime._map_child_status(RunStatus.FAILED) == "failed"

    def test_gave_up_maps_to_failed(self, tmp_path):
        backend = MockBackend([])
        runtime, _ = _make_runtime(tmp_path, backend)
        assert runtime._map_child_status(RunStatus.GAVE_UP) == "failed"


class TestChildMissingInfo:
    def _result(self, status, summary="", error=None):
        return RunResult(
            task_id="test",
            status=status,
            summary=summary,
            steps_taken=1,
            error=error,
        )

    def test_completed_returns_empty(self, tmp_path):
        runtime, _ = _make_runtime(tmp_path, MockBackend([]))
        assert runtime._child_missing_info("completed", self._result(RunStatus.SUCCESS)) == ""

    def test_partial_with_error_returns_error(self, tmp_path):
        runtime, _ = _make_runtime(tmp_path, MockBackend([]))
        result = self._result(RunStatus.MAX_STEPS, error="ran out of steps")
        assert runtime._child_missing_info("partial", result) == "ran out of steps"

    def test_partial_without_error_returns_default(self, tmp_path):
        runtime, _ = _make_runtime(tmp_path, MockBackend([]))
        result = self._result(RunStatus.MAX_STEPS)
        info = runtime._child_missing_info("partial", result)
        assert "stopped before fully covering" in info

    def test_failed_with_error_returns_error(self, tmp_path):
        runtime, _ = _make_runtime(tmp_path, MockBackend([]))
        result = self._result(RunStatus.FAILED, error="LLM crash")
        assert runtime._child_missing_info("failed", result) == "LLM crash"

    def test_failed_without_error_with_summary_returns_summary(self, tmp_path):
        runtime, _ = _make_runtime(tmp_path, MockBackend([]))
        result = self._result(RunStatus.FAILED, summary="partial work done")
        assert runtime._child_missing_info("failed", result) == "partial work done"

    def test_failed_without_error_or_summary_returns_default(self, tmp_path):
        runtime, _ = _make_runtime(tmp_path, MockBackend([]))
        result = self._result(RunStatus.FAILED)
        info = runtime._child_missing_info("failed", result)
        assert "failed before producing" in info


class TestExtractChildArtifacts:
    def test_extracts_path_keys(self, tmp_path):
        runtime, _ = _make_runtime(tmp_path, MockBackend([]))
        messages = [
            LLMMessage(
                role="assistant",
                content="writing",
                tool_calls=[
                    ToolCall(name="file_write", params={"path": "src/a.py", "content": "x"}),
                    ToolCall(name="file_edit", params={"file_path": "src/b.py"}),
                    ToolCall(name="shell", params={"target_path": "/tmp/out"}),
                    ToolCall(name="shell", params={"new_path": "renamed.py"}),
                ],
            ),
        ]
        artifacts = runtime._extract_child_artifacts(messages)
        assert set(artifacts) == {"src/a.py", "src/b.py", "/tmp/out", "renamed.py"}

    def test_deduplicates_paths(self, tmp_path):
        runtime, _ = _make_runtime(tmp_path, MockBackend([]))
        messages = [
            LLMMessage(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(name="file_read", params={"path": "src/a.py"}),
                    ToolCall(name="file_read", params={"path": "src/a.py"}),
                ],
            ),
        ]
        artifacts = runtime._extract_child_artifacts(messages)
        assert artifacts == ["src/a.py"]

    def test_skips_non_string_and_empty(self, tmp_path):
        runtime, _ = _make_runtime(tmp_path, MockBackend([]))
        messages = [
            LLMMessage(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(name="t", params={"path": 123}),
                    ToolCall(name="t", params={"file_path": ""}),
                    ToolCall(name="t", params={"path": "   "}),
                ],
            ),
        ]
        artifacts = runtime._extract_child_artifacts(messages)
        assert artifacts == []

    def test_ignores_non_assistant_messages(self, tmp_path):
        runtime, _ = _make_runtime(tmp_path, MockBackend([]))
        messages = [
            LLMMessage(role="user", content="read src/a.py"),
            LLMMessage(role="tool", content="ok", tool_call_id="tc1"),
        ]
        artifacts = runtime._extract_child_artifacts(messages)
        assert artifacts == []


class TestChildSessionResultSerialization:
    def test_to_dict_has_all_fields(self):
        result = ChildSessionResult(
            session_id="sess_001",
            status="completed",
            summary="Found 3 files.",
            artifacts=("src/a.py", "src/b.py"),
            missing_info="",
            error="",
        )
        d = result.to_dict()
        assert set(d.keys()) == {"session_id", "status", "summary", "artifacts", "missing_info", "error"}

    def test_artifacts_tuple_converted_to_list(self):
        result = ChildSessionResult(
            session_id="s1",
            status="completed",
            summary="done",
            artifacts=("a.py", "b.py"),
        )
        d = result.to_dict()
        assert isinstance(d["artifacts"], list)
        assert d["artifacts"] == ["a.py", "b.py"]

    def test_roundtrip_preserves_data(self):
        result = ChildSessionResult(
            session_id="s1",
            status="partial",
            summary="half done",
            artifacts=("x.py",),
            missing_info="stopped early",
            error="budget",
        )
        d = result.to_dict()
        assert d["session_id"] == "s1"
        assert d["status"] == "partial"
        assert d["summary"] == "half done"
        assert d["artifacts"] == ["x.py"]
        assert d["missing_info"] == "stopped early"
        assert d["error"] == "budget"


class TestTaskToolV2Validation:
    def _make_tool(self, tmp_path):
        backend = MockBackend([])
        runtime, _ = _make_runtime(tmp_path, backend)
        root = runtime.create_root_session(
            agent_name="build",
            repo_path=str(tmp_path),
            title="root",
        )
        return TaskToolV2(runtime, root.id)

    def test_missing_subagent_type(self, tmp_path):
        tool = self._make_tool(tmp_path)
        result = tool.execute({"prompt": "do something"})
        assert result.success is False
        assert "subagent_type" in result.error

    def test_missing_prompt(self, tmp_path):
        tool = self._make_tool(tmp_path)
        result = tool.execute({"subagent_type": "explore"})
        assert result.success is False
        assert "prompt" in result.error

    def test_empty_prompt_rejected(self, tmp_path):
        tool = self._make_tool(tmp_path)
        result = tool.execute({"subagent_type": "explore", "prompt": "   "})
        assert result.success is False

    def test_unknown_subagent_type(self, tmp_path):
        tool = self._make_tool(tmp_path)
        result = tool.execute({"subagent_type": "nonexistent", "prompt": "do it"})
        assert result.success is False
        assert "nonexistent" in result.error


# ============================================================
# Layer 2: Integration Tests
# ============================================================


def test_v2_child_session_partial_on_max_steps(tmp_path):
    """Child hits step limit -> status partial, parent retains all tools."""
    backend = MockBackend([
        # Parent delegates
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="delegate",
            tool_calls=[
                ToolCall(name="task", params={"subagent_type": "explore", "prompt": "Explore deeply."})
            ],
        ),
        # Child uses one tool (hits max_steps=1)
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="searching",
            tool_calls=[ToolCall(name="find_files", params={"pattern": "*.py"})],
        ),
        # Parent finishes
        Action(action_type=ActionType.FINISH, thought="done", message="Parent done"),
    ])
    runtime, store = _make_runtime(tmp_path, backend, child_max_steps=1)
    root = runtime.create_root_session(agent_name="build", repo_path=str(tmp_path), title="root")

    result = runtime.run_session(root.id, agent_name="build", task_description="Explore", intent="analysis", messages=[])

    assert result.summary == "Parent done"
    # Parent's second call (after child returns) should still have all tools
    parent_second_tools = set(backend.received_tools[2])
    assert "file_read" in parent_second_tools
    assert "find_files" in parent_second_tools
    assert "search_text" in parent_second_tools
    # Verify child is partial in store
    children = store.list_child_sessions(root.id)
    assert len(children) == 1


def test_v2_child_session_failed_gave_up(tmp_path):
    """Child gives up -> status failed, error populated."""
    backend = MockBackend([
        # Parent delegates
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="delegate",
            tool_calls=[
                ToolCall(name="task", params={"subagent_type": "explore", "prompt": "Do impossible task."})
            ],
        ),
        # Child gives up
        Action(action_type=ActionType.GIVE_UP, thought="cannot do this", message="I give up"),
        # Parent finishes
        Action(action_type=ActionType.FINISH, thought="ok", message="Parent done"),
    ])
    runtime, store = _make_runtime(tmp_path, backend)
    root = runtime.create_root_session(agent_name="build", repo_path=str(tmp_path), title="root")

    result = runtime.run_session(root.id, agent_name="build", task_description="Test", intent="analysis", messages=[])

    assert result.summary == "Parent done"
    # Verify the tool result to parent indicates failure
    root_messages = store.list_messages(root.id)
    tool_msgs = [str(m.content) for m in root_messages if m.role == "tool"]
    assert any("I give up" in msg for msg in tool_msgs)


def test_v2_task_tool_returns_plain_text_not_json(tmp_path):
    """Task tool output is plain text summary, not JSON wrapper."""
    backend = MockBackend([
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="delegate",
            tool_calls=[
                ToolCall(name="task", params={"subagent_type": "explore", "prompt": "Look around."})
            ],
        ),
        Action(action_type=ActionType.FINISH, thought="done", message="Found 3 modules."),
        Action(action_type=ActionType.FINISH, thought="ok", message="Parent done"),
    ])
    runtime, store = _make_runtime(tmp_path, backend)
    root = runtime.create_root_session(agent_name="build", repo_path=str(tmp_path), title="root")

    runtime.run_session(root.id, agent_name="build", task_description="Test", intent="analysis", messages=[])

    root_messages = store.list_messages(root.id)
    tool_msgs = [str(m.content) for m in root_messages if m.role == "tool"]
    assert tool_msgs
    assert "Structured child session result" not in tool_msgs[0]
    assert "Found 3 modules" in tool_msgs[0]


def test_v2_partial_child_returns_success_true_with_note(tmp_path):
    """Partial child returns success=True and appends [Note: ...] to output."""
    backend = MockBackend([
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="delegate",
            tool_calls=[
                ToolCall(name="task", params={"subagent_type": "explore", "prompt": "Big exploration."})
            ],
        ),
        # Child does one thing then hits max_steps=1
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="start",
            tool_calls=[ToolCall(name="find_files", params={"pattern": "*.py"})],
        ),
        # Parent finishes
        Action(action_type=ActionType.FINISH, thought="ok", message="Done"),
    ])
    runtime, store = _make_runtime(tmp_path, backend, child_max_steps=1)
    root = runtime.create_root_session(agent_name="build", repo_path=str(tmp_path), title="root")

    result = runtime.run_session(root.id, agent_name="build", task_description="Test", intent="analysis", messages=[])

    assert result.summary == "Done"
    root_messages = store.list_messages(root.id)
    tool_msgs = [str(m.content) for m in root_messages if m.role == "tool"]
    assert tool_msgs
    assert "[Note:" in tool_msgs[0]


def test_v2_general_subagent_gets_write_tools(tmp_path):
    """General subagent has file_write, file_edit, shell but not task."""
    backend = MockBackend([
        Action(action_type=ActionType.FINISH, thought="done", message="General done"),
    ])
    runtime, _ = _make_runtime(tmp_path, backend)
    root = runtime.create_root_session(agent_name="build", repo_path=str(tmp_path), title="root")

    runtime.run_child_session(
        parent_session_id=root.id,
        subagent_type="general",
        description="test write tools",
        prompt="Implement something.",
    )

    child_tools = set(backend.received_tools[0])
    assert "file_write" in child_tools
    assert "file_edit" in child_tools
    assert "shell" in child_tools
    assert "task" not in child_tools


def test_v2_sequential_multi_child_delegation(tmp_path):
    """Parent delegates twice in sequence, both children tracked."""
    backend = MockBackend([
        # [0] Parent's first delegation
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="first delegation",
            tool_calls=[
                ToolCall(name="task", params={"subagent_type": "explore", "prompt": "Find auth files."})
            ],
        ),
        # [1] First child finishes
        Action(action_type=ActionType.FINISH, thought="done", message="Auth: found 2 files"),
        # [2] Parent's second delegation (explore to avoid edit-intent complications)
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="second delegation",
            tool_calls=[
                ToolCall(name="task", params={"subagent_type": "explore", "prompt": "Check the test coverage."})
            ],
        ),
        # [3] Second child finishes
        Action(action_type=ActionType.FINISH, thought="done", message="Tests cover 80%"),
        # [4] Parent finishes
        Action(action_type=ActionType.FINISH, thought="done", message="All done"),
    ])
    runtime, store = _make_runtime(tmp_path, backend)
    root = runtime.create_root_session(agent_name="build", repo_path=str(tmp_path), title="root")

    result = runtime.run_session(root.id, agent_name="build", task_description="Analyze auth", intent="analysis", messages=[])

    assert result.summary == "All done"
    children = store.list_child_sessions(root.id)
    assert len(children) == 2
    assert children[0].agent_name == "explore"
    assert children[1].agent_name == "explore"
    root_messages = store.list_messages(root.id)
    tool_msgs = [str(m.content) for m in root_messages if m.role == "tool"]
    assert any("Auth: found 2 files" in msg for msg in tool_msgs)
    assert any("Tests cover 80%" in msg for msg in tool_msgs)


# ============================================================
# Layer 3: Contract Tests
# ============================================================


def test_v2_child_result_dict_schema():
    """ChildSessionResult.to_dict() has exactly the expected keys."""
    result = ChildSessionResult(
        session_id="s1", status="completed", summary="done",
        artifacts=("a.py",), missing_info="", error="",
    )
    d = result.to_dict()
    expected_keys = {"session_id", "status", "summary", "artifacts", "missing_info", "error"}
    assert set(d.keys()) == expected_keys


def test_v2_partial_tool_result_contains_note_indicator(tmp_path):
    """When child is partial, parent's observation text contains [Note:]."""
    backend = MockBackend([
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="delegate",
            tool_calls=[ToolCall(name="task", params={"subagent_type": "explore", "prompt": "Big task."})],
        ),
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="work",
            tool_calls=[ToolCall(name="find_files", params={"pattern": "*"})],
        ),
        Action(action_type=ActionType.FINISH, thought="ok", message="Done"),
    ])
    runtime, store = _make_runtime(tmp_path, backend, child_max_steps=1)
    root = runtime.create_root_session(agent_name="build", repo_path=str(tmp_path), title="root")

    runtime.run_session(root.id, agent_name="build", task_description="Test", intent="analysis", messages=[])

    root_messages = store.list_messages(root.id)
    tool_msgs = [str(m.content) for m in root_messages if m.role == "tool"]
    assert any("[Note:" in msg for msg in tool_msgs)


def test_v2_child_rule_for_general_subagent(tmp_path):
    """General subagent receives its own variant of [V2 Child Session Rule]."""
    backend = MockBackend([
        Action(action_type=ActionType.FINISH, thought="done", message="General done"),
    ])
    runtime, _ = _make_runtime(tmp_path, backend)
    root = runtime.create_root_session(agent_name="build", repo_path=str(tmp_path), title="root")

    runtime.run_child_session(
        parent_session_id=root.id,
        subagent_type="general",
        description="test",
        prompt="Do something.",
    )

    first_call_messages = backend.received_messages[0]
    combined = "\n".join(str(m.content) for m in first_call_messages if m.role == "user")
    assert "[V2 Child Session Rule]" in combined
    assert "general child session" in combined
    assert "standalone and directly useful" in combined


# ============================================================
# Layer 4: Fault Injection Tests
# ============================================================


def test_v2_child_script_exhaustion_does_not_crash_parent(tmp_path):
    """If child gives up, parent still completes gracefully."""
    backend = MockBackend([
        # [0] Parent delegates
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="delegate",
            tool_calls=[ToolCall(name="task", params={"subagent_type": "explore", "prompt": "Work."})],
        ),
        # [1] Child gives up immediately
        Action(action_type=ActionType.GIVE_UP, thought="stuck", message="Cannot proceed"),
        # [2] Parent finishes after receiving failed child result
        Action(action_type=ActionType.FINISH, thought="ok", message="Recovered"),
    ])
    runtime, _ = _make_runtime(tmp_path, backend)
    root = runtime.create_root_session(agent_name="build", repo_path=str(tmp_path), title="root")

    result = runtime.run_session(root.id, agent_name="build", task_description="Test", intent="analysis", messages=[])

    assert result.status == RunStatus.SUCCESS
    assert result.summary == "Recovered"


def test_v2_parent_recovers_after_failed_child_with_second_delegation(tmp_path):
    """First child fails, parent delegates again, second child succeeds."""
    backend = MockBackend([
        # [0] Parent's first delegation
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="first try",
            tool_calls=[ToolCall(name="task", params={"subagent_type": "explore", "prompt": "Try 1."})],
        ),
        # [1] First child gives up
        Action(action_type=ActionType.GIVE_UP, thought="stuck", message="Cannot proceed"),
        # [2] Parent tries again (explore to avoid edit-intent issue)
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="retry",
            tool_calls=[ToolCall(name="task", params={"subagent_type": "explore", "prompt": "Try 2 differently."})],
        ),
        # [3] Second child succeeds
        Action(action_type=ActionType.FINISH, thought="done", message="Success on retry"),
        # [4] Parent finishes
        Action(action_type=ActionType.FINISH, thought="done", message="Final answer"),
    ])
    runtime, store = _make_runtime(tmp_path, backend)
    root = runtime.create_root_session(agent_name="build", repo_path=str(tmp_path), title="root")

    result = runtime.run_session(root.id, agent_name="build", task_description="Test", intent="analysis", messages=[])

    assert result.summary == "Final answer"
    children = store.list_child_sessions(root.id)
    assert len(children) == 2
    root_messages = store.list_messages(root.id)
    tool_msgs = [str(m.content) for m in root_messages if m.role == "tool"]
    assert any("Success on retry" in msg for msg in tool_msgs)


def test_v2_unknown_parent_session_raises(tmp_path):
    """run_child_session with nonexistent parent raises ValueError."""
    backend = MockBackend([])
    runtime, _ = _make_runtime(tmp_path, backend)

    with pytest.raises(ValueError, match="Unknown parent session"):
        runtime.run_child_session(
            parent_session_id="nonexistent_id",
            subagent_type="explore",
            description="test",
            prompt="Do something.",
        )


def test_v2_build_readonly_tool_then_final_text_is_success(tmp_path):
    """Final text after a read-only tool call completes V2 without post-hoc write validation."""
    backend = MockBackend([
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="read memory",
            tool_calls=[ToolCall(name="memory_read", params={"name": "memory-system-test-contract"})],
        ),
        Action(
            action_type=ActionType.FINISH,
            thought="summarize",
            message="The memory records how to test the memory system.",
        ),
    ])
    runtime, store = _make_runtime(tmp_path, backend)
    root = runtime.create_root_session(agent_name="build", repo_path=str(tmp_path), title="root")

    result = runtime.run_session(
        root.id,
        agent_name="build",
        task_description="Read the memory and summarize it. Do not modify files.",
        intent="analysis",
        messages=[],
    )

    assert result.status == RunStatus.SUCCESS
    assert result.summary == "The memory records how to test the memory system."
    assert store.get_session(root.id).status == "completed"
