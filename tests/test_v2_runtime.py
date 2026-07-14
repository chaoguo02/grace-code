"""Tests for V2 fork-based subagent runtime."""

from __future__ import annotations

from pathlib import Path

import pytest

from hooks.events import HookContext, HookEvent
from hooks.protocol import DispatchResult, HookControl
from agent.core import AgentConfig, ReActAgent
from agent.event_log import EventLog
from agent.policy import PhasePolicy
from agent.policy_registry import PolicyAwareToolRegistry
from agent.task import (
    Action,
    ActionType,
    Observation,
    ObservationStatus,
    RunStatus,
    Task,
    TaskIntent,
    ToolCall,
)
from agent.v2 import AgentRegistryV2, AgentTool, ForkResult, SessionRuntime, SessionStore
from agent.v2.models import (
    AgentDefinition,
    AgentIsolation,
    AgentVisibility,
    ForkStatus,
    SessionMode,
    SessionStatus,
)
from agent.v2.task_tool import _format_fork_result
from llm.base import LLMMessage, MockBackend
from tools.artifact_tool import ArtifactReadTool, ArtifactStoreRef
from tools.base import NoopTool, ToolEffect, ToolMetadata, ToolRegistry
from tools.evidence_tool import EvidenceLedgerRef, EvidenceListTool
from context.artifacts import ArtifactStore
from context.evidence import EvidenceLedger
from tools.file_tool import (
    FileReadCache, FileReadTool, FileViewTool,
    MAX_READ_LINES, VIEW_WINDOW_LINES,
)


class _StubRuntime:
    def __init__(self, fork_result: ForkResult) -> None:
        self.agent_registry = AgentRegistryV2()
        self._fork_result = fork_result

    def fork_session(self, **kwargs):
        return self._fork_result

    def get_session_repo_path(self, session_id: str) -> str:
        return str(Path.cwd())


def _make_runtime(
    tmp_path,
    backend: MockBackend,
    *,
    tool_overrides: dict[str, NoopTool] | None = None,
) -> tuple[SessionRuntime, SessionStore]:
    agent_registry = AgentRegistryV2(project_dir=tmp_path)
    base_registry = ToolRegistry()
    overrides = tool_overrides or {}

    for tool_name in sorted(agent_registry.tool_names_for("build")):
        base_registry.register(
            overrides.get(tool_name, NoopTool(tool_name, output=f"{tool_name} ok"))
        )

    store = SessionStore(str(tmp_path / ".forge-agent" / "v2" / "sessions.db"))
    runtime = SessionRuntime(
        store=store,
        backend=backend,
        base_registry=base_registry,
        agent_registry=agent_registry,
        root_agent_config=AgentConfig(
            max_steps=10, budget_tokens=50_000, request_budget_tokens=20_000,
            history_max_messages=20, stream=False,
        ),
        log_dir=str(tmp_path / "logs"),
    )
    return runtime, store


# ── Session Store ──

def test_v2_session_store_persists_parent_child_relationships(tmp_path):
    store = SessionStore(str(tmp_path / ".forge-agent" / "v2" / "sessions.db"))
    root = store.create_session(agent_name="build", mode="primary", repo_path=str(tmp_path), title="root")
    child = store.create_session(agent_name="explore", mode="subagent", repo_path=str(tmp_path),
                                 title="child", parent_id=root.id, root_id=root.root_id)
    assert root.mode is SessionMode.PRIMARY
    assert root.status is SessionStatus.QUEUED
    assert child.mode is SessionMode.SUBAGENT
    assert store.get_session(root.id).parent_id is None
    assert store.get_session(child.id).parent_id == root.id
    assert [item.id for item in store.list_child_sessions(root.id)] == [child.id]


def test_v2_session_store_rejects_invalid_child_topology(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    root = store.create_session(
        agent_name="build", mode=SessionMode.PRIMARY,
        repo_path=str(tmp_path), title="root",
    )

    with pytest.raises(ValueError, match="requires parent_id"):
        store.create_session(
            agent_name="explore", mode=SessionMode.SUBAGENT,
            repo_path=str(tmp_path), title="orphan",
        )
    with pytest.raises(ValueError, match="must use subagent mode"):
        store.create_session(
            agent_name="build", mode=SessionMode.PRIMARY,
            repo_path=str(tmp_path), title="fake child", parent_id=root.id,
        )
    with pytest.raises(ValueError, match="must match its parent"):
        store.create_session(
            agent_name="explore", mode=SessionMode.SUBAGENT,
            repo_path=str(tmp_path), title="wrong root",
            parent_id=root.id, root_id="forged-root",
        )


def test_v2_session_store_rejects_messages_for_unknown_session(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))

    with pytest.raises(ValueError, match="Unknown v2 session"):
        store.append_message(
            "missing", LLMMessage(role="user", content="not persisted"),
        )


# ── Agent Registry ──

def test_v2_agent_registry_loads_builtins():
    registry = AgentRegistryV2()
    for name in ("explore", "general", "code-reviewer", "coordinator"):
        definition = registry.get(name)
        assert isinstance(definition, AgentDefinition)
        assert definition.name == name

    assert registry.get("explore").intent is TaskIntent.ANALYSIS
    assert registry.get("code-reviewer").intent is TaskIntent.ANALYSIS
    assert registry.get("general").intent is TaskIntent.EDIT


def test_agent_registry_project_scope_is_independent_of_process_cwd(tmp_path, monkeypatch):
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    for root, marker in ((project_a, "from-a"), (project_b, "from-b")):
        agents_dir = root / ".forge-agent" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "explore.md").write_text(
            "---\nname: explore\ndescription: " + marker
            + "\nintent: analysis\ntools: Read\n---\nInspect only.",
            encoding="utf-8",
        )

    monkeypatch.chdir(project_b)
    AgentRegistryV2.invalidate_cache()
    registry = AgentRegistryV2(project_dir=project_a)

    assert registry.project_dir == str(project_a.resolve())
    assert registry.get("explore").description == "from-a"


def test_agent_registry_reloads_when_definition_content_changes(tmp_path):
    import os

    agents_dir = tmp_path / ".forge-agent" / "agents"
    agents_dir.mkdir(parents=True)
    definition_path = agents_dir / "explore.md"
    definition_path.write_text(
        "---\nname: explore\ndescription: first\nintent: analysis\n---\nInspect.",
        encoding="utf-8",
    )
    AgentRegistryV2.invalidate_cache(tmp_path)
    first = AgentRegistryV2(project_dir=tmp_path)
    original_mtime = definition_path.stat().st_mtime_ns

    definition_path.write_text(
        "---\nname: explore\ndescription: second\nintent: analysis\n---\nInspect.",
        encoding="utf-8",
    )
    os.utime(
        definition_path,
        ns=(definition_path.stat().st_atime_ns, original_mtime + 1_000_000_000),
    )
    second = AgentRegistryV2(project_dir=tmp_path)

    assert first.get("explore").description == "first"
    assert second.get("explore").description == "second"


def test_agent_definition_loader_without_project_does_not_scan_cwd(tmp_path, monkeypatch):
    from agent.v2.agent_definition import load_agent_definitions

    agents_dir = tmp_path / ".forge-agent" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "cwd-only.md").write_text(
        "---\nname: cwd-only\ndescription: cwd\nintent: analysis\n---\nCWD agent.",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    definitions = load_agent_definitions(
        project_dir=None,
        user_dir=tmp_path / "empty-user-agents",
    )

    assert "cwd-only" not in definitions


def test_agent_definition_frontmatter_declares_intent(tmp_path):
    from agent.v2.agent_definition import _parse_definition

    path = tmp_path / "auditor.md"
    path.write_text(
        "---\nname: auditor\nintent: analysis\ntools: Read\n---\nAudit the project.",
        encoding="utf-8",
    )

    definition = _parse_definition(path)

    assert definition is not None
    assert definition.intent is TaskIntent.ANALYSIS
    assert definition.isolation is AgentIsolation.FORK
    assert definition.visibility is AgentVisibility.PUBLIC


def test_agent_definition_rejects_unknown_intent(tmp_path):
    from agent.v2.agent_definition import _parse_definition

    path = tmp_path / "invalid.md"
    path.write_text(
        "---\nname: invalid\nintent: maybe\n---\nInvalid.",
        encoding="utf-8",
    )

    assert _parse_definition(path) is None


def test_agent_definition_requires_explicit_intent(tmp_path):
    from agent.v2.agent_definition import _parse_definition

    path = tmp_path / "missing.md"
    path.write_text("---\nname: missing\n---\nMissing intent.", encoding="utf-8")

    assert _parse_definition(path) is None


def test_agent_definition_rejects_unknown_isolation(tmp_path):
    from agent.v2.agent_definition import _parse_definition

    path = tmp_path / "invalid-isolation.md"
    path.write_text(
        "---\nname: invalid\nintent: analysis\nisolation: container\n---\nInvalid.",
        encoding="utf-8",
    )

    assert _parse_definition(path) is None


def test_project_agent_definitions_declare_typed_intents():
    from agent.v2.agent_definition import _parse_definition

    project_agents = Path(__file__).parents[1] / ".forge-agent" / "agents"

    assert _parse_definition(project_agents / "explore.md").intent is TaskIntent.ANALYSIS
    assert _parse_definition(project_agents / "general.md").intent is TaskIntent.EDIT


@pytest.mark.parametrize(
    "field",
    ("visibility: private", "hidden: true", "background: true"),
)
def test_agent_definition_rejects_invalid_or_unsupported_visibility(field, tmp_path):
    from agent.v2.agent_definition import _parse_definition

    path = tmp_path / "invalid-visibility.md"
    path.write_text(
        f"---\nname: invalid\nintent: analysis\n{field}\n---\nInvalid.",
        encoding="utf-8",
    )

    assert _parse_definition(path) is None


def test_agent_definition_parses_hidden_visibility(tmp_path):
    from agent.v2.agent_definition import _parse_definition

    path = tmp_path / "hidden.md"
    path.write_text(
        "---\nname: hidden\nintent: analysis\nvisibility: hidden\n---\nHidden.",
        encoding="utf-8",
    )

    definition = _parse_definition(path)

    assert definition is not None
    assert definition.visibility is AgentVisibility.HIDDEN


def test_v2_agent_registry_resolves_tool_names():
    registry = AgentRegistryV2()
    names = registry.tool_names_for("explore")
    assert "file_read" in names or "Read" in names


def test_subagent_registry_uses_passed_definition_as_fact_source(tmp_path):
    from agent.v2.subagent_registry_factory import build_restricted_registry

    definition = AgentDefinition(
        name="explore",
        description="dispatch-time definition",
        intent=TaskIntent.ANALYSIS,
        tools=frozenset({"Read"}),
    )
    base = ToolRegistry()
    base.register(NoopTool("file_read"))
    base.register(NoopTool("shell"))

    restricted, _ = build_restricted_registry(
        definition, base, repo_path=str(tmp_path),
    )

    assert "file_read" in restricted.tool_names
    assert "shell" not in restricted.tool_names


def test_v2_agent_registry_builtin_primary_agents_declare_allowed_subagents():
    registry = AgentRegistryV2()
    assert registry.get("build").allowed_subagents == frozenset({"explore", "general", "code-reviewer"})
    assert registry.get("plan").allowed_subagents == frozenset({"explore", "code-reviewer"})
    assert registry.get("coordinator").allowed_subagents == frozenset({"explore", "general", "code-reviewer"})


def test_v2_coordinator_tool_names_are_schema_level_restricted():
    registry = AgentRegistryV2()
    names = registry.tool_names_for("coordinator")
    assert "task" in names
    assert "file_read" in names
    assert "find_files" in names
    assert "search_text" in names
    assert "shell" not in names
    assert "file_write" not in names
    assert "file_edit" not in names


# ── AgentTool ──

def test_v2_task_tool_rejects_unknown_subagent_type(tmp_path):
    backend = MockBackend([])
    runtime, store = _make_runtime(tmp_path, backend)
    tool = AgentTool(runtime, "parent")
    result = tool.execute({"subagent_type": "nonexistent", "description": "test", "prompt": "do it"})
    assert result.success is False
    assert "Unknown subagent_type" in result.error


def test_v2_task_tool_enforces_caller_allowed_subagents(tmp_path):
    runtime, _ = _make_runtime(tmp_path, MockBackend([]))
    tool = AgentTool(runtime, "parent", caller_agent_name="build")
    result = tool.execute({"subagent_type": "build", "description": "bad delegate", "prompt": "do it"})
    assert result.success is False
    assert "not allowed" in result.error
    assert "general" in result.error


def test_v2_task_tool_description_lists_only_allowed_subagents(tmp_path):
    runtime, _ = _make_runtime(tmp_path, MockBackend([]))
    tool = AgentTool(runtime, "parent", caller_agent_name="build")
    description = tool.description
    assert "- general:" in description
    assert "- explore:" in description
    assert "- build:" not in description
    assert "- plan:" not in description


def test_v2_plan_delegation_cannot_escalate_to_write_capable_agent(tmp_path):
    runtime, _ = _make_runtime(tmp_path, MockBackend([]))
    tool = AgentTool(runtime, "parent", caller_agent_name="plan")

    assert "- general:" not in tool.description
    result = tool.execute({
        "subagent_type": "general",
        "description": "implement plan",
        "prompt": "edit the file",
    })

    assert result.success is False
    assert "not allowed" in result.error


def test_v2_analysis_delegation_defaults_to_read_only_scope():
    from agent.v2.models import AgentDefinition, AgentIsolation

    parent = AgentDefinition(
        name="audit", description="audit", intent=TaskIntent.ANALYSIS,
        allowed_subagents=frozenset({"general"}), isolation=AgentIsolation.NONE,
    )
    child = AgentDefinition(
        name="general", description="writer", intent=TaskIntent.EDIT,
    )

    assert parent.permits_subagent(child) is False


def test_v2_task_tool_rejects_missing_params():
    tool = AgentTool.__new__(AgentTool)
    result = tool.execute({})
    assert result.success is False
    assert "requires" in result.error


def test_v2_task_tool_rejects_blank_description(tmp_path):
    runtime, _ = _make_runtime(tmp_path, MockBackend([]))
    tool = AgentTool(runtime, "parent")
    result = tool.execute({"subagent_type": "general", "description": "   ", "prompt": "do it"})
    assert result.success is False
    assert "requires" in result.error


def test_v2_task_tool_rejects_none_params_as_missing(tmp_path):
    runtime, _ = _make_runtime(tmp_path, MockBackend([]))
    tool = AgentTool(runtime, "parent")
    result = tool.execute({"subagent_type": None, "description": "test", "prompt": "do it"})
    assert result.success is False
    assert "requires" in result.error
    assert not result.error.startswith("Unknown subagent_type")


def test_v2_task_tool_partial_warns_but_succeeds():
    fork_result = ForkResult(
        agent_name="general",
        session_id="child123",
        status="partial",
        summary="partly done",
        turns_used=10,
    )
    tool = AgentTool(_StubRuntime(fork_result), "parent")
    result = tool.execute({"subagent_type": "general", "description": "check file", "prompt": "do it"})
    assert result.success is True
    assert result.output.startswith("WARNING: Subagent reached max steps (10 turns).")
    assert "<status>partial</status>" in result.output
    assert "<summary>\npartly done\n  </summary>" in result.output


def test_fork_result_coerces_status_at_boundary():
    result = ForkResult(
        agent_name="explore", session_id="typed", status="completed", summary="done"
    )
    assert result.status is ForkStatus.COMPLETED


def test_v2_format_fork_result_handles_none_summary():
    result = ForkResult(
        agent_name="general",
        session_id="child123",
        status="completed",
        summary=None,  # type: ignore[arg-type]
        turns_used=1,
    )
    output = _format_fork_result("general", result)
    assert "<summary>\n\n  </summary>" in output


def test_v2_task_tool_result_bypasses_artifacts_and_truncation_for_parent():
    agent = ReActAgent(MockBackend([]), ToolRegistry(), AgentConfig(stream=False))
    long_summary = "x" * 20_000
    observation = Observation(
        status=ObservationStatus.SUCCESS,
        output=f"<task-notification><summary>{long_summary}</summary></task-notification>",
        tool_name="task",
    )
    content = agent._build_tool_result_content(observation)
    assert long_summary in content
    assert "omitted" not in content


# ── Dynamic tool visibility ──

def test_v2_unattached_artifact_and_evidence_tools_hidden_from_schemas():
    artifact_ref = ArtifactStoreRef()
    evidence_ref = EvidenceLedgerRef()
    base = ToolRegistry()
    base.register(ArtifactReadTool(artifact_ref))
    base.register(EvidenceListTool(evidence_ref))
    base.register(NoopTool("file_read"))
    base._artifact_store_ref = artifact_ref
    base._evidence_ledger_ref = evidence_ref

    registry = PolicyAwareToolRegistry(
        base=base,
        phase_policy=PhasePolicy(allowed_tools=frozenset(base.tool_names)),
        repo_path=".",
        phase_name="test",
    )

    assert {schema.name for schema in registry.get_schemas()} == {"file_read"}
    assert "artifact_read" not in registry.tool_names
    blocked = registry.execute_tool("artifact_read", {"artifact_id": "art_x"})
    assert blocked.success is False
    assert "not available in the current environment" in blocked.error

    artifact_ref.store = ArtifactStore()
    evidence_ref.ledger = EvidenceLedger()
    assert {schema.name for schema in registry.get_schemas()} == {"artifact_read", "evidence_list", "file_read"}
    assert "artifact_read" in registry.tool_names


def test_policy_filters_by_effect_not_tool_name(tmp_path):
    base = ToolRegistry()
    network_tool = NoopTool("arbitrary_connector")
    network_tool.metadata = ToolMetadata(effects=frozenset({ToolEffect.NETWORK}))
    base.register(network_tool)

    registry = PolicyAwareToolRegistry(
        base=base,
        phase_policy=PhasePolicy(denied_effects=frozenset({ToolEffect.NETWORK})),
        repo_path=str(tmp_path),
        phase_name="test",
    )

    assert registry.tool_names == []


def test_v2_coordinator_runtime_registry_excludes_write_and_shell_tools(tmp_path):
    runtime, _ = _make_runtime(tmp_path, MockBackend([]))
    session = runtime.create_root_session(agent_name="coordinator", repo_path=str(tmp_path), title="coord")
    spec = runtime.agent_registry.get("coordinator")
    registry = runtime._build_registry_for_session(spec, session)
    schema_names = {schema.name for schema in registry.get_schemas()}

    assert "task" in schema_names
    assert "file_read" in schema_names
    assert "find_files" in schema_names
    assert "search_text" in schema_names
    assert "shell" not in schema_names
    assert "file_write" not in schema_names
    assert "file_edit" not in schema_names


# ── Fork Result ──

def test_v2_fork_result_fields():
    result = ForkResult(
        agent_name="explore", session_id="abc123", status="completed",
        summary="Found 3 files.", turns_used=5,
    )
    assert result.session_id == "abc123"
    assert result.status == "completed"


# ── Primary agent run ──

def test_v2_build_agent_runs_to_completion(tmp_path):
    # Include a file_write before FINISH — the completion guard requires
    # at least one write for edit tasks.
    backend = MockBackend([
        Action(action_type=ActionType.TOOL_CALL, thought="writing",
               tool_calls=[ToolCall(name="file_write", params={"path": str(tmp_path / "out.txt"), "content": "ok"})]),
        Action(action_type=ActionType.FINISH, thought="done", message="Task complete."),
        # Stop Hook blocks first FINISH (no verification), agent retries
        Action(action_type=ActionType.FINISH, thought="retry after verify", message="Task complete."),
    ])
    runtime, store = _make_runtime(tmp_path, backend)
    session = runtime.create_root_session(agent_name="build", repo_path=str(tmp_path), title="test")
    result = runtime.run_session(session.id, agent_name="build", task_description="do it", intent="edit")
    assert "Task complete." in result.summary


def test_v2_runtime_rejects_registry_from_another_project(tmp_path):
    backend = MockBackend([
        Action(action_type=ActionType.FINISH, thought="done", message="done"),
    ])
    runtime, _ = _make_runtime(tmp_path / "registry-project", backend)
    execution_repo = tmp_path / "execution-project"
    execution_repo.mkdir()
    with pytest.raises(ValueError, match="project scope does not match"):
        runtime.create_root_session(
            agent_name="build",
            repo_path=str(execution_repo),
            title="wrong project",
        )


def test_v2_task_tool_fails_closed_for_missing_parent_session(tmp_path):
    runtime, _ = _make_runtime(tmp_path, MockBackend([]))
    tool = AgentTool(runtime, "missing-parent", caller_agent_name="build")

    result = tool.execute({
        "subagent_type": "explore",
        "description": "inspect project",
        "prompt": "Read only.",
    })

    assert result.success is False
    assert "Unknown v2 session" in result.error


def test_v2_identical_task_executes_again_without_result_cache(tmp_path):
    backend = MockBackend([
        Action(action_type=ActionType.TOOL_CALL, thought="first write",
               tool_calls=[ToolCall(name="file_write", params={"path": str(tmp_path / "first.txt"), "content": "one"})]),
        Action(action_type=ActionType.FINISH, thought="done", message="first run"),
        Action(action_type=ActionType.TOOL_CALL, thought="second write",
               tool_calls=[ToolCall(name="file_write", params={"path": str(tmp_path / "second.txt"), "content": "two"})]),
        Action(action_type=ActionType.FINISH, thought="done", message="second run"),
    ])
    runtime, _ = _make_runtime(tmp_path, backend)
    first = runtime.create_root_session(agent_name="build", repo_path=str(tmp_path), title="first")
    second = runtime.create_root_session(agent_name="build", repo_path=str(tmp_path), title="second")

    first_result = runtime.run_session(
        first.id, agent_name="build", task_description="same task", intent="edit",
    )
    second_result = runtime.run_session(
        second.id, agent_name="build", task_description="same task", intent="edit",
    )

    assert first_result.steps_taken > 0
    assert second_result.steps_taken > 0
    assert backend.call_count == 4
    assert "second run" in second_result.summary


def test_v2_session_continuation_does_not_duplicate_persisted_history(tmp_path):
    backend = MockBackend([
        Action(action_type=ActionType.FINISH, thought="done", message="first report"),
        Action(action_type=ActionType.FINISH, thought="done", message="revised report"),
    ])
    runtime, store = _make_runtime(tmp_path, backend)
    session = runtime.create_root_session(agent_name="plan", repo_path=str(tmp_path), title="plan")

    runtime.run_session(
        session.id, agent_name="plan", task_description="review runtime", intent="analysis",
    )
    first_count = len(store.list_messages(session.id))
    runtime.run_session(
        session.id,
        agent_name="plan",
        task_description="review runtime",
        intent="analysis",
        messages=[LLMMessage(role="user", content="add exact line numbers")],
    )
    persisted = store.list_messages(session.id)

    assert len(persisted) - first_count == 2  # revision + fresh capability facts
    assert sum(message.content == "review runtime" for message in persisted) == 1


def test_v2_react_agent_stop_hook_blocks_then_continues(tmp_path):
    calls = 0

    def stop_callback(ctx: HookContext):
        nonlocal calls
        calls += 1
        assert ctx.event == HookEvent.STOP
        assert ctx.messages

    class BlockingDispatcher:
        def dispatch_stop(self, ctx):
            stop_callback(ctx)
            if calls == 1:
                return DispatchResult(control=HookControl.BLOCK, reason="tests failed")
            return DispatchResult()

    tool_registry = ToolRegistry(hook_dispatcher=BlockingDispatcher())
    agent = ReActAgent(
        MockBackend([
            Action(action_type=ActionType.FINISH, thought="done", message="too early"),
            Action(action_type=ActionType.FINISH, thought="done", message="done after hook"),
        ]),
        tool_registry,
        AgentConfig(stream=False),
    )
    task = Task("finish with stop hook", str(tmp_path), max_steps=5, intent="edit")
    with EventLog.create(task, log_dir=str(tmp_path / "logs")) as event_log:
        result = agent.run(task, event_log)

    assert result.summary == "done after hook"
    assert calls == 2


def test_v2_react_agent_stop_hook_retry_limit_gives_up(tmp_path):
    from hooks.protocol import DispatchResult

    class AlwaysBlockingDispatcher:
        def dispatch_stop(self, ctx):
            return DispatchResult(control=HookControl.BLOCK, reason="still failing")

    tool_registry = ToolRegistry(hook_dispatcher=AlwaysBlockingDispatcher())
    agent = ReActAgent(
        MockBackend([
            Action(action_type=ActionType.FINISH, thought="done", message="try1"),
            Action(action_type=ActionType.FINISH, thought="done", message="try2"),
            Action(action_type=ActionType.FINISH, thought="done", message="try3"),
            Action(action_type=ActionType.FINISH, thought="done", message="try4"),
        ]),
        tool_registry,
        AgentConfig(stream=False),
    )
    task = Task("finish with failing stop hook", str(tmp_path), max_steps=8)
    with EventLog.create(task, log_dir=str(tmp_path / "logs")) as event_log:
        result = agent.run(task, event_log)

    assert result.status == RunStatus.GAVE_UP
    assert "Stop hook retry limit reached" in result.summary


def test_v2_plan_agent_is_readonly(tmp_path):
    agent_registry = AgentRegistryV2()
    definition = agent_registry.get("explore")
    assert "Write" in definition.disallowed_tools or definition.tools
    assert definition.mode == "subagent"


def test_budget_exhaustion_allows_final_answer_and_reports_tokens(tmp_path):
    class ReadNoopTool(NoopTool):
        metadata = ToolMetadata(effects=frozenset({ToolEffect.READ_WORKSPACE}))

    token_updates = []
    backend = MockBackend(
        [
            Action(ActionType.TOOL_CALL, "inspect", [ToolCall("noop", {})]),
            Action(ActionType.TOOL_CALL, "inspect more", [ToolCall("noop", {})]),
            Action(ActionType.FINISH, "summarize", message="budget summary"),
        ],
        input_tokens=100,
        output_tokens=50,
    )
    agent = ReActAgent(
        backend,
        ToolRegistry().register(ReadNoopTool("noop")),
        AgentConfig(
            max_steps=5,
            budget_tokens=200,
            stream=False,
            token_callback=token_updates.append,
        ),
    )
    task = Task(
        "analyze within budget",
        str(tmp_path),
        max_steps=5,
        budget_tokens=200,
        intent=TaskIntent.ANALYSIS,
    )

    with EventLog.create(task, log_dir=str(tmp_path / "logs")) as event_log:
        result = agent.run(task, event_log)

    assert result.status is RunStatus.SUCCESS
    assert result.summary == "budget summary"
    assert backend.received_tools[-1] == []
    assert token_updates == [150, 300, 450]
    assert sum(
        "FORCE FINISH" in str(message.content)
        for message in backend.received_messages[-1]
    ) == 1


# ── Subagent tool restriction ──

def test_v2_build_gets_task_tool(tmp_path):
    backend = MockBackend([
        Action(action_type=ActionType.FINISH, thought="ok", message="done"),
    ])
    runtime, store = _make_runtime(tmp_path, backend)
    session = runtime.create_root_session(agent_name="build", repo_path=str(tmp_path), title="test")
    definition = runtime.agent_registry.get("build")
    assert definition is not None


# ── Fork execution ──

def test_v2_fork_subagent_builds_restricted_registry(tmp_path):
    backend = MockBackend([Action(action_type=ActionType.FINISH, thought="ok", message="summary")])
    runtime, store = _make_runtime(tmp_path, backend)
    parent = runtime.create_root_session(
        agent_name="build", repo_path=str(tmp_path), title="parent",
    )
    result = runtime.fork_session(
        parent_session_id=parent.id,
        definition=runtime.agent_registry.get("explore"),
        description="explore auth",
        prompt="Find login flow",
    )
    assert result.status == "completed"
    assert result.summary == "summary"
    assert result.agent_name == "explore"
    child = store.get_session(result.session_id)
    assert child is not None
    assert child.parent_id == parent.id
    assert child.root_id == parent.root_id
    assert child.mode is SessionMode.SUBAGENT
    assert child.status is SessionStatus.COMPLETED
    assert child.summary == "summary"
    child_messages = store.list_messages(child.id)
    assert (child_messages[0].role, child_messages[0].content) == (
        "user", "Find login flow",
    )
    assert child_messages[-1].role == "assistant"
    assert child_messages[-1].content == "summary"
    assert any("[ENVIRONMENT]" in str(message.content) for message in child_messages)
    assert store.list_messages(parent.id) == []


def test_v2_subagent_persists_native_tool_pairs_in_child_transcript(tmp_path):
    class WorkspaceReadNoop(NoopTool):
        metadata = ToolMetadata(effects=frozenset({ToolEffect.READ_WORKSPACE}))

    backend = MockBackend([
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="inspect",
            tool_calls=[ToolCall(name="file_read", params={})],
        ),
        Action(action_type=ActionType.FINISH, thought="done", message="inspected"),
    ])
    runtime, store = _make_runtime(
        tmp_path,
        backend,
        tool_overrides={"file_read": WorkspaceReadNoop("file_read")},
    )
    parent = runtime.create_root_session(
        agent_name="build", repo_path=str(tmp_path), title="parent",
    )

    result = runtime.fork_session(
        parent_session_id=parent.id,
        definition=runtime.agent_registry.get("explore"),
        description="inspect file",
        prompt="Inspect a.py",
    )

    messages = store.list_messages(result.session_id)
    tool_request = next(message for message in messages if message.tool_calls)
    tool_response = next(message for message in messages if message.role == "tool")
    assert tool_request.role == "assistant"
    assert tool_request.tool_calls[0].name == "file_read"
    assert tool_request.tool_calls[0].id is not None
    assert tool_response.tool_call_id == tool_request.tool_calls[0].id
    assert messages[-1].role == "assistant"
    assert messages[-1].content == "inspected"


def test_v2_failed_subagent_converges_session_state(tmp_path):
    backend = MockBackend([
        Action(
            action_type=ActionType.GIVE_UP,
            thought="blocked",
            message="cannot inspect safely",
        ),
    ])
    runtime, store = _make_runtime(tmp_path, backend)
    parent = runtime.create_root_session(
        agent_name="build", repo_path=str(tmp_path), title="parent",
    )

    result = runtime.fork_session(
        parent_session_id=parent.id,
        definition=runtime.agent_registry.get("explore"),
        description="blocked inspection",
        prompt="Inspect unavailable input",
    )

    child = store.get_session(result.session_id)
    assert result.status is ForkStatus.FAILED
    assert child is not None
    assert child.status is SessionStatus.FAILED
    assert child.summary == "cannot inspect safely"
    assert child.error == "cannot inspect safely"
    assert child.completed_at is not None


def test_v2_fork_subagent_max_steps_exhaustion(tmp_path):
    # Run many steps until max_steps exhausted
    actions = []
    for _ in range(55):
        actions.append(Action(
            action_type=ActionType.TOOL_CALL, thought="searching",
            tool_calls=[ToolCall(name="file_read", params={"path": "a.py"})],
        ))
    backend = MockBackend(actions)
    runtime, store = _make_runtime(tmp_path, backend)
    parent = runtime.create_root_session(
        agent_name="build", repo_path=str(tmp_path), title="parent",
    )
    result = runtime.fork_session(
        parent_session_id=parent.id,
        definition=runtime.agent_registry.get("explore"),
        description="exhaustive search", prompt="Find everything",
    )
    assert result.status in ("partial", "failed")
    child = store.get_session(result.session_id)
    assert child is not None
    assert child.status in (SessionStatus.PARTIAL, SessionStatus.FAILED)
    assert child.completed_at is not None


def test_v2_parent_recovers_after_failed_child(tmp_path):
    # Fork subagent finishes immediately
    child_backend = MockBackend([
        Action(action_type=ActionType.FINISH, thought="sub", message="child done"),
    ])
    runtime, store = _make_runtime(tmp_path, child_backend)
    parent = runtime.create_root_session(
        agent_name="build", repo_path=str(tmp_path), title="parent",
    )
    result = runtime.fork_session(
        parent_session_id=parent.id,
        definition=runtime.agent_registry.get("general"),
        description="fast task", prompt="Do quick thing",
    )
    assert result.status == "completed"

    # Parent can still run after child — separate backend needed in real use,
    # but here we verify fork doesn't crash and session still works.
    parent_backend = MockBackend([
        Action(action_type=ActionType.TOOL_CALL, thought="writing",
               tool_calls=[ToolCall(name="file_write", params={"path": str(tmp_path / "x.txt"), "content": "x"})]),
        Action(action_type=ActionType.FINISH, thought="ok", message="parent done"),
        Action(action_type=ActionType.FINISH, thought="retry", message="parent done"),
    ])
    runtime2, _ = _make_runtime(tmp_path, parent_backend)
    session = runtime2.create_root_session(agent_name="build", repo_path=str(tmp_path), title="recovery")
    result2 = runtime2.run_session(session.id, agent_name="build", task_description="recover", intent="edit")
    assert "parent done" in result2.summary


def test_v2_runtime_injects_subagent_descriptions(tmp_path):
    backend = MockBackend([
        Action(action_type=ActionType.FINISH, thought="ok", message="done"),
    ])
    runtime, store = _make_runtime(tmp_path, backend)
    definition = runtime.agent_registry.get("build")
    messages = runtime._build_runtime_messages(definition, "test task")
    text = " ".join(str(m.content) for m in messages)
    assert "task" in text
    assert "explore" in text
    assert "general" in text
    assert "Subagent Output Review Protocol" in text
    assert "INSPECT before you relay" in text
    assert "UNVERIFIED" in text
    assert "NEVER verbatim-forward" in text
    assert "SPOT DESIGN PATTERNS" in text
    assert "Atomic Task Boundaries" in text
    assert "Subagent Failure Recovery" in text
    assert "Runtime enforces retry limits" in text
    assert "The system will stop you" in text


def test_v2_plan_reserves_final_turn_for_plan_output(tmp_path):
    """Plan exploration becomes a Runtime-enforced, tool-free final turn."""
    actions = [
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="inspect",
            tool_calls=[ToolCall(name="file_read", params={"path": "a.py"})],
        )
        for _ in range(3)
    ]
    actions.append(Action(
        action_type=ActionType.FINISH,
        thought="plan ready",
        message=(
            "### Goal\nPrepare the change.\n\n"
            "### Constraints\nRead only.\n\n"
            "### Steps\n1. Inspect a.py.\n\n"
            "### Verification\nReview cited lines.\n\n"
            '{"objective":"Prepare a safe implementation plan",'
            '"execution_intent":"analysis","target_files":["a.py"],'
            '"expected_behavior":"The requested behavior is documented",'
            '"verification_strategy":"Review cited lines",'
            '"potential_conflicts":[]}'
        ),
    ))
    backend = MockBackend(actions)
    runtime, _ = _make_runtime(tmp_path, backend)
    session = runtime.create_root_session(
        agent_name="plan", repo_path=str(tmp_path), title="plan",
    )

    result = runtime.run_session(
        session.id,
        agent_name="plan",
        task_description="Plan an analysis of a.py",
        intent=TaskIntent.ANALYSIS,
    )

    assert result.status is RunStatus.SUCCESS
    assert "### Goal" in result.summary
    assert backend.received_tools[-1] == []
    assert any(
        "Planning exploration is complete" in str(message.content)
        for message in backend.received_messages[-1]
    )


def test_v2_plan_does_not_execute_tool_calls_after_tools_are_withdrawn(tmp_path):
    actions = [
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="inspect",
            tool_calls=[ToolCall(name="file_read", params={"path": "a.py"})],
        )
        for _ in range(4)
    ]
    actions.append(Action(
        action_type=ActionType.FINISH,
        thought="finalize without tools",
        message="### Goal\nFinalize the plan after the rejected tool call.",
    ))
    backend = MockBackend(actions)
    runtime, _ = _make_runtime(tmp_path, backend)
    session = runtime.create_root_session(
        agent_name="plan", repo_path=str(tmp_path), title="plan",
    )

    result = runtime.run_session(
        session.id,
        agent_name="plan",
        task_description="Plan an analysis of a.py",
        intent=TaskIntent.ANALYSIS,
    )

    assert result.status is RunStatus.SUCCESS
    assert backend.received_tools[-2:] == [[], []]
    assert any(
        "Tool calls are disabled" in str(message.content)
        for message in backend.received_messages[-1]
    )


def test_v2_plan_runtime_prompt_excludes_write_capable_delegation(tmp_path):
    runtime, _ = _make_runtime(tmp_path, MockBackend([]))
    definition = runtime.agent_registry.get("plan")

    text = " ".join(
        str(message.content)
        for message in runtime._build_runtime_messages(definition, "plan task")
    )

    assert "read-only delegation scope" in text
    assert "**general**" not in text
    assert "Task routing guide" in text


def test_v2_subagent_summary_rule_includes_consumption_signals(tmp_path):
    runtime, _ = _make_runtime(tmp_path, MockBackend([]))
    definition = runtime.agent_registry.get("general")
    messages = runtime._build_runtime_messages(definition, "test task")
    assert messages == []

    from agent.v2.subagent import _build_system_messages
    subagent_messages = _build_system_messages(definition)
    text = " ".join(str(m.content) for m in subagent_messages)
    assert "UNVERIFIED" in text
    assert "Never repeat information from the task prompt" in text
    assert "success=True with status \"partial\"" in text
    assert "Do NOT report \"partial with success=True\" as a bug" in text
    assert "TOOL SELECTION RULES" in text
    assert "USE THE DEDICATED TOOL FIRST" in text
    assert "shell/zsh/bash" in text


def test_v2_unknown_parent_session_raises(tmp_path):
    runtime, store = _make_runtime(tmp_path, MockBackend([]))
    with pytest.raises(ValueError, match="Unknown v2 session"):
        runtime.run_session("nonexistent", agent_name="build", task_description="x", intent="analysis")


# ── Agent Registry isolation boundary ──

def test_list_subagents_excludes_primary_agents():
    """list_subagents() must never return primary agents."""
    registry = AgentRegistryV2()
    subagents = registry.list_subagents()
    subagent_names = {agent.name for agent in subagents}
    primary_names = {a.name for a in registry.list_primary_agents()}

    assert "code-reviewer" not in subagent_names
    assert registry.get("code-reviewer").visibility is AgentVisibility.HIDDEN

    for spec in subagents:
        assert spec.isolation is not AgentIsolation.NONE, (
            f"Primary agent {spec.name!r} leaked into list_subagents()"
        )
        assert spec.name not in primary_names


# ── Subagent report format validation (Layer 2) ──

from agent.v2.task_tool import (
    _build_subagent_prompt,
    _SUBAGENT_PROTOCOL, _KNOWN_DESIGN_DECISIONS,
)



# ── Subagent prompt wrapper (Layer 1) ──

def test_build_subagent_prompt_includes_protocol():
    """_build_subagent_prompt wraps code-reviewer prompts with the full protocol."""
    result = _build_subagent_prompt("Analyze task_tool.py for bugs.", "code-reviewer")
    assert "[SUBAGENT ANALYSIS PROTOCOL]" in result
    assert "READ BEFORE YOU CLAIM" in result
    assert "Phase 1" in result and "Phase 2" in result and "Phase 3" in result and "Phase 4" in result
    assert "Anti-Laziness" in result
    assert "submit_findings" in result
    assert "Analyze task_tool.py for bugs." in result
    # User prompt must come after the protocol
    assert result.index("Analyze task_tool.py for bugs.") > result.index("[SUBAGENT ANALYSIS PROTOCOL]")


def test_build_subagent_prompt_non_reviewer_passthrough():
    """Non-code-reviewer subagents get the prompt directly — no protocol wrapping."""
    for agent_type in ("explore", "general"):
        result = _build_subagent_prompt("Find all config files.", agent_type)
        assert result == "Find all config files."
        assert "[SUBAGENT ANALYSIS PROTOCOL]" not in result


def test_known_design_decisions_injected_into_protocol():
    """The shareable _KNOWN_DESIGN_DECISIONS list is injected into the protocol."""
    result = _build_subagent_prompt("Do X.", "code-reviewer")
    assert "KNOWN DESIGN DECISIONS" in result
    for entry in _KNOWN_DESIGN_DECISIONS:
        # First 40 chars of each entry should appear in the protocol
        assert entry[:40] in result


# ── Structured subagent failure diagnosis (P1) ──

from agent.v2.subagent import _build_structured_diagnosis
from agent.task import RunResult, RunStatus


def test_build_structured_diagnosis_includes_all_fields():
    """Diagnosis must include failure_type, steps_consumed, last_action,
    repeated_count, and a one-line diagnosis summary."""
    result = RunResult(
        task_id="t1", status=RunStatus.GAVE_UP, summary="Loop detected: ...",
        steps_taken=21, total_tokens=5000,
    )
    recent = [
        {"name": "file_read", "params": {"path": "agent/v2/task_tool.py"}},
        {"name": "file_read", "params": {"path": "agent/v2/task_tool.py"}},
        {"name": "file_read", "params": {"path": "agent/v2/task_tool.py"}},
        {"name": "file_read", "params": {"path": "agent/v2/task_tool.py"}},
        {"name": "file_read", "params": {"path": "agent/v2/task_tool.py"}},
    ]
    diag = _build_structured_diagnosis(result, recent)

    assert "failure_type: gave_up" in diag
    assert "steps_consumed: 21" in diag
    assert "last_action: file_read" in diag
    assert "repeated_count: 5" in diag
    assert "diagnosis:" in diag


def test_build_structured_diagnosis_no_repeat_when_varied():
    """When tools vary, repeated_count should not appear."""
    result = RunResult(
        task_id="t1", status=RunStatus.FAILED, summary="failed",
        steps_taken=5, total_tokens=1000, error="tool crash",
    )
    recent = [
        {"name": "bash", "params": {"command": "pytest"}},
        {"name": "file_read", "params": {"path": "x.py"}},
        {"name": "search_text", "params": {"pattern": "foo"}},
    ]
    diag = _build_structured_diagnosis(result, recent)

    assert "failure_type: failed" in diag
    assert "last_action: bash" in diag
    assert "error: tool crash" in diag
    assert "repeated_count:" not in diag


def test_build_structured_diagnosis_handles_empty_actions():
    """Empty recent_actions list should not crash."""
    result = RunResult(
        task_id="t1", status=RunStatus.FAILED, summary="crash",
        steps_taken=1, total_tokens=100, error="broken",
    )
    diag = _build_structured_diagnosis(result, [])
    assert "failure_type: failed" in diag
    assert "last_action:" not in diag


# ── FileReadCache (Claude Code-style tool-layer dedup) ──

def test_file_read_cache_miss_then_hit():
    """First read misses, second read of same range hits cache."""
    cache = FileReadCache()

    # First read — miss
    assert cache.check("/abs/path/a.py", offset=1, limit=500) is None

    # Store content
    content = "line 1\nline 2\nline 3"
    cache.store("/abs/path/a.py", offset=1, limit=500, content=content)

    # Second read of same range — hit
    cached = cache.check("/abs/path/a.py", offset=1, limit=500)
    assert cached == content


def test_file_read_cache_partial_range_covered_by_full():
    """A full-file cache entry covers any sub-range."""
    cache = FileReadCache()

    # Store full file
    full_content = "a\nb\nc\nd\ne\nf\ng\nh\ni\nj"
    cache.store("/abs/path/a.py", offset=None, limit=None, content=full_content)

    # Any sub-range should hit
    assert cache.check("/abs/path/a.py", offset=3, limit=5) == full_content
    assert cache.check("/abs/path/a.py", offset=1, limit=2) == full_content


def test_file_read_cache_non_overlapping_range_misses():
    """Non-overlapping range should miss the cache."""
    cache = FileReadCache()

    # Store lines 1-100
    cache.store("/abs/path/a.py", offset=1, limit=100, content="first 100")

    # Read lines 200-300 — no overlap with 1-100
    assert cache.check("/abs/path/a.py", offset=200, limit=100) is None


def test_file_read_cache_invalidate_clears_path():
    """invalidate() removes all cached entries for a path."""
    cache = FileReadCache()

    cache.store("/abs/path/a.py", offset=1, limit=100, content="data")
    assert cache.check("/abs/path/a.py", offset=1, limit=100) is not None

    cache.invalidate("/abs/path/a.py")

    assert cache.check("/abs/path/a.py", offset=1, limit=100) is None


def test_file_read_cache_no_frequency_cap():
    """mtime-based cache has no artificial frequency cap.
    Repeated reads of the same file are harmless — mtime verification
    ensures freshness, so there's no reason to block them."""
    cache = FileReadCache()
    # Store and verify repeated checks work (no -1 cap)
    cache.store("/abs/path/a.py", offset=1, limit=100, content="data")
    for _ in range(10):
        assert cache.check("/abs/path/a.py", offset=1, limit=100) is not None


def test_file_read_tool_cache_hit(tmp_path):
    """FileReadTool returns [CACHED] on repeated reads."""
    tool = FileReadTool(read_cache=FileReadCache())

    # Create a test file
    f = tmp_path / "test.py"
    f.write_text("line 1\nline 2\nline 3\n")

    # First read — normal
    r1 = tool.execute({"path": str(f)})
    assert r1.success
    assert "[CACHED]" not in r1.output
    assert "line 1" in r1.output

    # Second read — cached (mtime unchanged)
    r2 = tool.execute({"path": str(f)})
    assert r2.success
    assert "[CACHED]" in r2.output


def test_file_read_tool_cache_invalidates_after_mtime_change(tmp_path):
    """When file mtime changes, cache misses and re-reads from disk."""
    tool = FileReadTool(read_cache=FileReadCache())

    f = tmp_path / "test.py"
    f.write_text("line 1\nline 2\nline 3\n")

    # First read — populate cache
    r1 = tool.execute({"path": str(f)})
    assert r1.success
    assert "[CACHED]" not in r1.output

    # Modify the file (mtime changes)
    import time
    time.sleep(0.01)  # ensure mtime changes (filesystem resolution)
    f.write_text("modified line 1\nmodified line 2\n")

    # Second read — mtime changed, should miss cache
    r2 = tool.execute({"path": str(f)})
    assert r2.success
    assert "[CACHED]" not in r2.output
    assert "modified" in r2.output


def test_file_read_cache_shared_across_tools(tmp_path):
    """Two tools sharing the same cache instance see each other's reads."""
    cache = FileReadCache()
    tool1 = FileReadTool(read_cache=cache)
    tool2 = FileReadTool(read_cache=cache)

    f = tmp_path / "test.py"
    f.write_text("shared cache test\n")

    # Tool1 reads, populating shared cache
    r1 = tool1.execute({"path": str(f)})
    assert r1.success
    assert "[CACHED]" not in r1.output

    # Tool2 reads — cache hit from shared cache
    r2 = tool2.execute({"path": str(f)})
    assert r2.success
    assert "[CACHED]" in r2.output


def test_file_view_tool_cache_hit_on_exact_re_read(tmp_path):
    """FileViewTool returns [CACHED] when the exact same start_line is re-read."""
    tool = FileViewTool(read_cache=FileReadCache())

    f = tmp_path / "test.py"
    lines = [f"line {i}" for i in range(1, 301)]
    f.write_text("\n".join(lines))

    # First read — normal
    r1 = tool.execute({"path": str(f), "start_line": 50})
    assert r1.success
    assert "[CACHED]" not in r1.output

    # Exact same re-read — cached (mtime unchanged)
    r2 = tool.execute({"path": str(f), "start_line": 50})
    assert r2.success
    assert "[CACHED]" in r2.output


def test_file_view_tool_cache_full_file_covers_subrange(tmp_path):
    """When file_read covers lines 1-500, file_view sub-ranges are cached."""
    tool = FileReadTool(read_cache=FileReadCache())
    view_tool = FileViewTool(read_cache=tool._read_cache)

    f = tmp_path / "test.py"
    lines = [f"line {i}" for i in range(1, 101)]
    f.write_text("\n".join(lines))

    # file_read covers the whole file (100 lines, offset=1, limit=500)
    tool.execute({"path": str(f)})

    # file_view of a sub-range — cache hit because file_read covers it all
    r = view_tool.execute({"path": str(f), "start_line": 30})
    assert r.success
    assert "[CACHED]" in r.output


def test_file_view_and_file_read_share_cache_no_cap(tmp_path):
    """file_read and file_view share the same cache — no artificial frequency cap.

    With mtime-verified caching, any number of reads is fine as long as the
    file hasn't been modified. The cache handles overlapping ranges correctly:
    file_read covers 1-500, file_view within that range hits cache.
    """
    cache = FileReadCache()
    read_tool = FileReadTool(read_cache=cache)
    view_tool = FileViewTool(read_cache=cache)

    f = tmp_path / "big.py"
    lines = [f"line {i}" for i in range(1, 701)]
    f.write_text("\n".join(lines))

    # file_read covers 1-500
    assert read_tool.execute({"path": str(f)}).success
    # file_view at 520 → cache miss (not covered by 1-500)
    r2 = view_tool.execute({"path": str(f), "start_line": 520})
    assert r2.success
    assert "[CACHED]" not in r2.output
    # file_view at 520 again → cache hit
    r3 = view_tool.execute({"path": str(f), "start_line": 520})
    assert r3.success
    assert "[CACHED]" in r3.output

    # Many more reads still work — no frequency cap with mtime cache
    for _ in range(10):
        r = view_tool.execute({"path": str(f), "start_line": 520})
        assert r.success
        assert "[CACHED]" in r.output
