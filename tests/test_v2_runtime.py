from __future__ import annotations

import json

from agent.task import Action, ActionType, ToolCall
from agent.v2 import AgentRegistryV2, SessionRuntime, SessionStore
from llm.base import MockBackend
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
                        "description": "inspect auth flow",
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
    structured_tool_messages = [
        str(message.content)
        for message in root_messages
        if message.role == "tool" and "Structured child session result" in str(message.content)
    ]
    assert structured_tool_messages
    payload = json.loads(structured_tool_messages[0].split("\n", 1)[1])
    assert payload["session_id"] == child.id
    assert payload["status"] == "completed"
    assert payload["summary"] == "Child summary"
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
                        "description": "explore v2 chain in agent/v2 and entry/cli.py",
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


def test_v2_runtime_injects_delegation_rule_for_explicit_task_requests(tmp_path):
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
        task_description="Please use the task tool with subagent_type=explore to inspect agent/v2.",
        intent="analysis",
        messages=[],
    )

    first_call_messages = backend.received_messages[0]
    assert any(
        message.role == "user" and "[V2 Primary Session Rule]" in str(message.content)
        for message in first_call_messages
    )
    assert any(
        message.role == "user" and "[V2 Delegation Rule]" in str(message.content)
        for message in first_call_messages
    )
    combined_user_content = "\n".join(
        str(message.content) for message in first_call_messages if message.role == "user"
    )
    assert "dispatch a new, more specific child session" in combined_user_content
    assert "## Task Mode: Analysis" not in combined_user_content
    assert "Current phase:" not in combined_user_content
    assert "Allowed tools:" not in combined_user_content


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


def test_v2_primary_hides_broad_exploration_tools_after_partial_child_result(tmp_path):
    backend = MockBackend([
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="delegate",
            tool_calls=[
                ToolCall(
                    name="task",
                    params={
                        "description": "explore v2",
                        "subagent_type": "explore",
                        "prompt": "Explore agent/v2 and summarize.",
                    },
                )
            ],
        ),
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="child explores",
            tool_calls=[ToolCall(name="find_files", params={"pattern": "v2"})],
        ),
        Action(
            action_type=ActionType.FINISH,
            thought="parent done",
            message="Parent summary",
        ),
    ])
    runtime, _store = _make_runtime(tmp_path, backend, child_max_steps=1)
    root = runtime.create_root_session(
        agent_name="build",
        repo_path=str(tmp_path),
        title="root",
    )

    result = runtime.run_session(
        root.id,
        agent_name="build",
        task_description="Please use task to explore agent/v2 and summarize.",
        intent="analysis",
        messages=[],
    )

    assert result.summary == "Parent summary"
    assert len(backend.received_tools) >= 3
    parent_second_call_tools = set(backend.received_tools[2])
    assert "task" in parent_second_call_tools
    assert "file_read" not in parent_second_call_tools
    assert "file_view" not in parent_second_call_tools
    assert "find_files" not in parent_second_call_tools
    assert "find_symbol" not in parent_second_call_tools
    assert "search_text" not in parent_second_call_tools


def test_v2_delegation_block_returns_guidance_and_does_not_abort_on_retries(tmp_path):
    backend = MockBackend([
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="delegate",
            tool_calls=[
                ToolCall(
                    name="task",
                    params={
                        "description": "explore v2",
                        "subagent_type": "explore",
                        "prompt": "Explore agent/v2 and summarize.",
                    },
                )
            ],
        ),
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="child explores",
            tool_calls=[ToolCall(name="find_files", params={"pattern": "v2"})],
        ),
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="retry one",
            tool_calls=[ToolCall(name="file_read", params={"path": "agent/v2/runtime.py"})],
        ),
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="retry two",
            tool_calls=[ToolCall(name="file_read", params={"path": "entry/cli.py"})],
        ),
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="retry three",
            tool_calls=[ToolCall(name="search_text", params={"pattern": "v2", "path": "."})],
        ),
        Action(
            action_type=ActionType.FINISH,
            thought="done",
            message="Parent summary",
        ),
    ])
    runtime, store = _make_runtime(tmp_path, backend, child_max_steps=1)
    root = runtime.create_root_session(
        agent_name="build",
        repo_path=str(tmp_path),
        title="root",
    )

    result = runtime.run_session(
        root.id,
        agent_name="build",
        task_description="Please use task to explore agent/v2 and summarize.",
        intent="analysis",
        messages=[],
    )

    assert result.summary == "Parent summary"
    assert result.status.value == "success"
    root_messages = store.list_messages(root.id)
    guided_blocks = [
        str(message.content)
        for message in root_messages
        if message.role == "tool" and "BLOCKED: You are in child-delegation mode" in str(message.content)
    ]
    assert len(guided_blocks) >= 3
    assert all("DO NOT retry this tool." in block for block in guided_blocks)
    assert all("Dispatch a new, more specific child task" in block for block in guided_blocks)


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
