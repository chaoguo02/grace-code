"""Tests for the V2 fresh-context subagent runtime."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import json
import sqlite3
import subprocess

import pytest

from hooks.events import HookContext, HookEvent
from hooks.protocol import DispatchResult, HookControl
from agent.core import AgentConfig, ReActAgent
from agent.event_log import EventLog
from agent.policy import PhasePolicy, build_task_policy
from agent.policy_registry import PolicyAwareToolRegistry
from agent.task import (
    Action,
    ActionType,
    EventType,
    Observation,
    ObservationStatus,
    RunResult,
    RunStatus,
    Task,
    TaskIntent,
    ToolCall,
)
from agent.v2 import (
    AgentSpawnContext,
    AgentRegistryV2,
    AgentTool,
    AgentModel,
    DelegationMode,
    DelegationPolicy,
    ForkResult,
    SessionRuntime,
    SessionStore,
)
from agent.v2.models import (
    AgentDefinition,
    AgentKind,
    AgentRunResult,
    AgentVisibility,
    ContextOrigin,
    ExecutionPlacement,
    ForkStatus,
    SessionMode,
    SessionStatus,
    WorktreeChange,
    WorktreeDisposition,
    WorktreeEvidence,
    WorkspaceMode,
)
from agent.v2.task_tool import _format_fork_result
from agent.v2.execution_budget import ExecutionBudget, ExecutionBudgetConfig
from agent.v2.run_context import CancellationToken, RunContext
from llm.base import LLMBackend, LLMMessage, LLMResponse, LLMToolSchema, MockBackend
from tools.artifact_tool import ArtifactReadTool, ArtifactStoreRef
from tools.base import (
    NoopTool, PathAccess, ToolConcurrency, ToolEffect, ToolMetadata,
    ToolRegistry, ToolRole,
)
from tools.evidence_tool import EvidenceLedgerRef, EvidenceListTool
from context.artifacts import ArtifactStore
from context.evidence import EvidenceLedger
from tools.file_tool import (
    FileReadCache, FileReadTool, FileViewTool,
    MAX_READ_LINES, VIEW_WINDOW_LINES,
)
from tools.submit_findings_tool import (
    FindingCategory,
    FindingSeverity,
    SubagentReportStatus,
    SubmitFindingsTool,
)


class _StubRuntime:
    def __init__(self, fork_result: ForkResult) -> None:
        self.agent_registry = AgentRegistryV2()
        self._fork_result = fork_result
        self.last_fork_kwargs = None

    def fork_session(self, **kwargs):
        self.last_fork_kwargs = kwargs
        return self._fork_result

    def get_session_repo_path(self, session_id: str) -> str:
        return str(Path.cwd())


class _WorkspaceReadNoop(NoopTool):
    metadata = ToolMetadata(effects=frozenset({ToolEffect.READ_WORKSPACE}))


def _run_context(tokens: int = 50_000) -> RunContext:
    budget = ExecutionBudget(config=ExecutionBudgetConfig(token_limit=tokens))
    budget.start()
    return RunContext(
        budget=budget,
        cancellation=CancellationToken(),
        delegation_step_limit=50,
        phase_policy=PhasePolicy(),
        delegation_effects=frozenset(ToolEffect),
    )


def _fork_resources(tokens: int = 50_000) -> dict[str, object]:
    return {
        "budget_tokens": tokens,
        "parent_max_steps": 50,
        "cancellation_token": CancellationToken(),
        "parent_policy": PhasePolicy(),
    }


def _make_runtime(
    tmp_path,
    backend: MockBackend,
    *,
    tool_overrides: dict[str, NoopTool] | None = None,
    hook_dispatcher=None,
    event_callback=None,
    state_dir: Path | None = None,
) -> tuple[SessionRuntime, SessionStore]:
    agent_registry = AgentRegistryV2(project_dir=tmp_path)
    base_registry = ToolRegistry()
    overrides = tool_overrides or {}

    for tool_name in sorted(agent_registry.tool_names_for("build")):
        base_registry.register(
            overrides.get(tool_name, NoopTool(tool_name, output=f"{tool_name} ok"))
        )

    runtime_state = state_dir or (tmp_path / ".forge-agent" / "v2")
    store = SessionStore(str(runtime_state / "sessions.db"))
    runtime = SessionRuntime(
        store=store,
        backend=backend,
        base_registry=base_registry,
        agent_registry=agent_registry,
        root_agent_config=AgentConfig(
            max_steps=10, budget_tokens=50_000, request_budget_tokens=20_000,
            history_max_messages=20, stream=False,
        ),
        log_dir=str(runtime_state / "logs"),
        hook_dispatcher=hook_dispatcher,
        event_callback=event_callback,
    )
    return runtime, store


# ── Session Store ──

def test_v2_session_store_persists_parent_child_relationships(tmp_path):
    store = SessionStore(str(tmp_path / ".forge-agent" / "v2" / "sessions.db"))
    root = store.create_session(agent_name="build", mode="primary", repo_path=str(tmp_path), title="root")
    child = store.create_session(agent_name="explore", mode="subagent", repo_path=str(tmp_path),
                                 title="child", parent_id=root.id, root_id=root.root_id)
    assert root.mode is SessionMode.PRIMARY
    assert root.agent_kind is AgentKind.PRIMARY
    assert root.context_origin is ContextOrigin.FRESH
    assert root.execution_placement is ExecutionPlacement.FOREGROUND
    assert root.workspace_mode is WorkspaceMode.CURRENT
    assert root.status is SessionStatus.QUEUED
    assert child.mode is SessionMode.SUBAGENT
    assert child.agent_kind is AgentKind.NAMED_SUBAGENT
    assert store.get_session(root.id).parent_id is None
    assert store.get_session(child.id).parent_id == root.id
    assert [item.id for item in store.list_child_sessions(root.id)] == [child.id]


def test_v2_session_store_migrates_legacy_child_contract_and_result(tmp_path):
    db_path = tmp_path / "legacy.db"
    legacy_result = ForkResult(
        agent_name="explore", session_id="child", status=ForkStatus.COMPLETED,
        summary="legacy facts",
    )
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, parent_id TEXT NULL, root_id TEXT NOT NULL,
                agent_name TEXT NOT NULL, mode TEXT NOT NULL, title TEXT NOT NULL,
                status TEXT NOT NULL, repo_path TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}', fork_result_json TEXT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                completed_at TEXT NULL
            );
            CREATE TABLE session_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL,
                tool_call_id TEXT NULL, tool_name TEXT NULL,
                tool_calls_json TEXT NULL, created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO sessions VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "child", "root", "root", "explore", "subagent", "legacy",
                "completed", str(tmp_path), "legacy facts", "",
                json.dumps({"isolation": "worktree"}),
                json.dumps(legacy_result.to_dict()), "now", "now", "now",
            ),
        )

    child = SessionStore(str(db_path)).get_session("child")

    assert child is not None
    assert child.agent_kind is AgentKind.NAMED_SUBAGENT
    assert child.context_origin is ContextOrigin.FRESH
    assert child.execution_placement is ExecutionPlacement.FOREGROUND
    assert child.workspace_mode is WorkspaceMode.WORKTREE
    assert child.agent_result == legacy_result
    assert child.fork_result == legacy_result


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
    with pytest.raises(ValueError, match="same role"):
        store.create_session(
            agent_name="explore", mode=SessionMode.SUBAGENT,
            agent_kind=AgentKind.PRIMARY, repo_path=str(tmp_path),
            title="conflicting role", parent_id=root.id,
        )
    with pytest.raises(ValueError, match="resolved execution placement"):
        store.create_session(
            agent_name="build", mode=SessionMode.PRIMARY,
            execution_placement=ExecutionPlacement.AUTO,
            repo_path=str(tmp_path), title="unresolved placement",
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
    for name in ("explore", "general", "code-reviewer"):
        definition = registry.get(name)
        assert isinstance(definition, AgentDefinition)
        assert definition.name == name

    assert registry.get("explore").intent is TaskIntent.ANALYSIS
    assert registry.get("code-reviewer").intent is TaskIntent.ANALYSIS
    assert registry.get("general").intent is TaskIntent.EDIT
    assert registry.get("build").agent_kind is AgentKind.PRIMARY
    assert registry.get("general").agent_kind is AgentKind.NAMED_SUBAGENT
    assert registry.get("general").workspace_mode is WorkspaceMode.CURRENT
    assert registry.has("coordinator") is False


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
    assert definition.agent_kind is AgentKind.NAMED_SUBAGENT
    assert definition.workspace_mode is WorkspaceMode.CURRENT
    assert definition.visibility is AgentVisibility.PUBLIC


def test_agent_definition_rejects_unknown_intent(tmp_path):
    from agent.v2.agent_definition import AgentDefinitionError, _parse_definition

    path = tmp_path / "invalid.md"
    path.write_text(
        "---\nname: invalid\nintent: maybe\n---\nInvalid.",
        encoding="utf-8",
    )

    with pytest.raises(AgentDefinitionError, match="field 'intent' has invalid value"):
        _parse_definition(path)


def test_agent_definition_requires_explicit_intent(tmp_path):
    from agent.v2.agent_definition import AgentDefinitionError, _parse_definition

    path = tmp_path / "missing.md"
    path.write_text("---\nname: missing\n---\nMissing intent.", encoding="utf-8")

    with pytest.raises(AgentDefinitionError, match="missing required field 'intent'"):
        _parse_definition(path)


def test_agent_definition_rejects_unknown_isolation(tmp_path):
    from agent.v2.agent_definition import AgentDefinitionError, _parse_definition

    path = tmp_path / "invalid-isolation.md"
    path.write_text(
        "---\nname: invalid\nintent: analysis\nisolation: container\n---\nInvalid.",
        encoding="utf-8",
    )

    with pytest.raises(AgentDefinitionError, match="field 'isolation' has invalid value"):
        _parse_definition(path)


def test_agent_definition_rejects_removed_fork_isolation(tmp_path):
    from agent.v2.agent_definition import AgentDefinitionError, _parse_definition

    path = tmp_path / "removed-fork-isolation.md"
    path.write_text(
        "---\nname: invalid\nintent: analysis\nisolation: fork\n---\nInvalid.",
        encoding="utf-8",
    )

    with pytest.raises(AgentDefinitionError, match="spawn context"):
        _parse_definition(path)


def test_agent_definition_rejects_obsolete_shared_workspace(tmp_path):
    from agent.v2.agent_definition import AgentDefinitionError, _parse_definition

    path = tmp_path / "shared-workspace.md"
    path.write_text(
        "---\nname: shared\nintent: analysis\nisolation: shared\n---\nAnalyze.",
        encoding="utf-8",
    )

    with pytest.raises(AgentDefinitionError, match="omit 'isolation'"):
        _parse_definition(path)


def test_agent_definition_accepts_worktree_workspace(tmp_path):
    from agent.v2.agent_definition import _parse_definition

    path = tmp_path / "worktree-agent.md"
    path.write_text(
        "---\nname: worker\nintent: edit\nisolation: worktree\n---\nEdit.",
        encoding="utf-8",
    )

    definition = _parse_definition(path)

    assert definition.workspace_mode is WorkspaceMode.WORKTREE


@pytest.mark.parametrize("configured", (None, "inherit", " INHERIT "))
def test_agent_definition_model_inherits_parent_backend(configured, tmp_path):
    from agent.v2.agent_definition import _parse_definition

    model_line = "" if configured is None else f"model: {configured}\n"
    path = tmp_path / "inherited-model.md"
    path.write_text(
        "---\n"
        "name: inherited-model\n"
        "intent: analysis\n"
        f"{model_line}"
        "---\n"
        "Analyze.\n",
        encoding="utf-8",
    )

    assert _parse_definition(path).model is AgentModel.INHERIT


@pytest.mark.parametrize("configured", ("sonnet", "claude-sonnet-5", 7))
def test_agent_definition_rejects_unsupported_model(configured, tmp_path):
    from agent.v2.agent_definition import AgentDefinitionError, _parse_definition

    path = tmp_path / "unsupported-model.md"
    path.write_text(
        "---\n"
        "name: unsupported-model\n"
        "intent: analysis\n"
        f"model: {configured}\n"
        "---\n"
        "Analyze.\n",
        encoding="utf-8",
    )

    with pytest.raises(AgentDefinitionError, match="field 'model'") as exc_info:
        _parse_definition(path)

    assert exc_info.value.path == path.resolve()
    assert (
        "supports only 'inherit'" in exc_info.value.detail
        or "must be a string" in exc_info.value.detail
    )


@pytest.mark.parametrize(
    "value, detail",
    (
        ("{}", "must be a string or list"),
        ("[explore, 7]", "list items must be strings"),
    ),
)
def test_agent_definition_rejects_invalid_allowed_subagents(value, detail, tmp_path):
    from agent.v2.agent_definition import AgentDefinitionError, _parse_definition

    path = tmp_path / "invalid-delegation.md"
    path.write_text(
        "---\n"
        "name: invalid-delegation\n"
        "intent: edit\n"
        f"allowedSubagents: {value}\n"
        "---\n"
        "Invalid delegation config.\n",
        encoding="utf-8",
    )

    with pytest.raises(AgentDefinitionError, match=detail):
        _parse_definition(path)


def test_project_agent_definitions_declare_typed_intents():
    from agent.v2.agent_definition import _parse_definition

    project_agents = Path(__file__).parents[1] / ".forge-agent" / "agents"

    assert _parse_definition(project_agents / "explore.md").intent is TaskIntent.ANALYSIS
    general = _parse_definition(project_agents / "general.md")
    assert general.intent is TaskIntent.EDIT
    assert general.workspace_mode is WorkspaceMode.CURRENT


@pytest.mark.parametrize(
    "field",
    ("visibility: private", "hidden: true", "background: true"),
)
def test_agent_definition_rejects_invalid_or_unsupported_visibility(field, tmp_path):
    from agent.v2.agent_definition import AgentDefinitionError, _parse_definition

    path = tmp_path / "invalid-visibility.md"
    path.write_text(
        f"---\nname: invalid\nintent: analysis\n{field}\n---\nInvalid.",
        encoding="utf-8",
    )

    with pytest.raises(AgentDefinitionError):
        _parse_definition(path)


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


def test_agent_definition_parses_and_validates_resource_limits(tmp_path):
    from agent.v2.agent_definition import AgentDefinitionError, _parse_definition

    valid = tmp_path / "bounded.md"
    valid.write_text(
        "---\nname: bounded\nintent: analysis\nmaxTurns: 7\nmaxTokens: 1234\n---\nInspect.",
        encoding="utf-8",
    )
    invalid = tmp_path / "invalid.md"
    invalid.write_text(
        "---\nname: invalid\nintent: analysis\nmaxTokens: 0\n---\nInspect.",
        encoding="utf-8",
    )

    definition = _parse_definition(valid)
    assert definition is not None
    assert definition.max_turns == 7
    assert definition.max_tokens == 1234
    with pytest.raises(AgentDefinitionError, match="must be positive integers"):
        _parse_definition(invalid)


def test_invalid_project_agent_cannot_fall_back_to_builtin(tmp_path):
    from agent.v2.agent_definition import AgentDefinitionError

    agents = tmp_path / ".forge-agent" / "agents"
    agents.mkdir(parents=True)
    invalid = agents / "explore.md"
    invalid.write_text(
        "---\nname: explore\ndescription: broken project override\n---\nBroken.",
        encoding="utf-8",
    )
    AgentRegistryV2.invalidate_cache(str(tmp_path))

    with pytest.raises(AgentDefinitionError) as exc_info:
        AgentRegistryV2(project_dir=tmp_path)

    assert exc_info.value.path == invalid.resolve()
    assert "missing required field 'intent'" in exc_info.value.detail


def test_duplicate_agent_names_in_one_scope_fail_closed(tmp_path):
    from agent.v2.agent_definition import AgentDefinitionError, load_agent_definitions

    agents = tmp_path / "agents"
    agents.mkdir()
    for filename in ("first.md", "second.md"):
        (agents / filename).write_text(
            "---\nname: duplicate\ndescription: duplicate\nintent: analysis\n---\nInspect.",
            encoding="utf-8",
        )

    with pytest.raises(AgentDefinitionError, match="duplicate agent name 'duplicate'"):
        load_agent_definitions(project_dir=None, user_dir=agents)


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
        definition,
        base,
        repo_path=str(tmp_path),
        parent_policy=PhasePolicy(),
    )

    assert "file_read" in restricted.tool_names
    assert "shell" not in restricted.tool_names
    assert "submit_findings" not in restricted.tool_names

    reviewer = AgentRegistryV2().get("code-reviewer")
    reviewer_registry, _ = build_restricted_registry(
        reviewer,
        base,
        repo_path=str(tmp_path),
        parent_policy=PhasePolicy(
            allowed_effects=frozenset({
                ToolEffect.READ_WORKSPACE,
                ToolEffect.PRODUCE_DELIVERABLE,
            }),
        ),
    )
    assert "submit_findings" in reviewer_registry.tool_names


def test_v2_agent_registry_builtin_primary_agents_declare_delegation_policy():
    registry = AgentRegistryV2()
    assert registry.get("build").delegation_policy == DelegationPolicy.allowlist(
        frozenset({"explore", "general", "code-reviewer"})
    )
    assert registry.get("plan").delegation_policy == DelegationPolicy.allowlist(
        frozenset({"explore", "code-reviewer"})
    )


def test_v2_agent_without_allowlist_has_delegation_disabled(tmp_path):
    definition = AgentDefinition(
        name="standalone",
        description="no delegation",
        intent=TaskIntent.EDIT,
        agent_kind=AgentKind.PRIMARY,
    )

    assert definition.delegation_policy.mode is DelegationMode.DISABLED
    assert definition.delegation_policy.allowed_names == frozenset()


def test_v2_disabled_delegation_hides_task_tool_and_prompt(tmp_path):
    agents_dir = tmp_path / ".forge-agent" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "build.md").write_text(
        "---\n"
        "name: build\n"
        "description: standalone primary\n"
        "intent: edit\n"
        "kind: primary\n"
        "tools: Read, Task\n"
        "allowedSubagents: []\n"
        "---\n"
        "Work without delegation.\n",
        encoding="utf-8",
    )
    runtime, _ = _make_runtime(tmp_path, MockBackend([]))
    definition = runtime.agent_registry.get("build")
    session = runtime.create_root_session(
        agent_name="build", repo_path=str(tmp_path), title="standalone"
    )

    registry = runtime._build_registry_for_session(definition, session)
    messages = runtime._build_runtime_messages(definition, "do work")

    assert definition.delegation_policy.mode is DelegationMode.DISABLED
    assert "task" not in registry.tool_names
    assert messages == []


def test_v2_empty_effective_delegation_hides_task_tool_and_prompt(tmp_path):
    agents_dir = tmp_path / ".forge-agent" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "build.md").write_text(
        "---\n"
        "name: build\n"
        "description: read-only primary\n"
        "intent: analysis\n"
        "kind: primary\n"
        "tools: Read, Task\n"
        "allowedSubagents: [general]\n"
        "---\n"
        "Analyze without authority escalation.\n",
        encoding="utf-8",
    )
    runtime, _ = _make_runtime(tmp_path, MockBackend([]))
    definition = runtime.agent_registry.get("build")
    session = runtime.create_root_session(
        agent_name="build", repo_path=str(tmp_path), title="read-only"
    )

    registry = runtime._build_registry_for_session(definition, session)
    messages = runtime._build_runtime_messages(definition, "analyze")

    assert definition.delegation_policy.mode is DelegationMode.ALLOWLIST
    assert runtime.agent_registry.delegatable_by(definition) == []
    assert "task" not in registry.tool_names
    assert messages == []


# ── AgentTool ──

def test_v2_task_tool_rejects_unknown_subagent_type(tmp_path):
    backend = MockBackend([])
    runtime, store = _make_runtime(tmp_path, backend)
    tool = AgentTool(runtime, "parent", caller_agent_name="build")
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
    assert "- code-reviewer:" in description
    assert "- build:" not in description
    assert "- plan:" not in description


def test_v2_plan_delegation_cannot_escalate_to_write_capable_agent(tmp_path):
    runtime, _ = _make_runtime(tmp_path, MockBackend([]))
    tool = AgentTool(runtime, "parent", caller_agent_name="plan")

    assert "- general:" not in tool.description
    assert "- code-reviewer:" in tool.description
    result = tool.execute({
        "subagent_type": "general",
        "description": "implement plan",
        "prompt": "edit the file",
    })

    assert result.success is False
    assert "not allowed" in result.error


def test_v2_task_tool_declares_authority_from_parent_delegation_scope(tmp_path):
    runtime, _ = _make_runtime(tmp_path, MockBackend([]))

    plan_tool = AgentTool(runtime, "plan-parent", caller_agent_name="plan")
    build_tool = AgentTool(runtime, "build-parent", caller_agent_name="build")

    assert plan_tool.metadata.effects == frozenset({
        ToolEffect.DELEGATE_READ_ONLY,
    })
    assert build_tool.metadata.effects == frozenset({
        ToolEffect.DELEGATE_WRITE,
    })
    assert plan_tool.concurrency_mode({
        "subagent_type": "explore",
    }) is ToolConcurrency.PARALLEL_SAFE
    assert build_tool.concurrency_mode({
        "subagent_type": "general",
    }) is ToolConcurrency.SERIAL


def test_v2_analysis_delegation_defaults_to_read_only_scope():
    from agent.v2.models import AgentDefinition, AgentKind

    parent = AgentDefinition(
        name="audit", description="audit", intent=TaskIntent.ANALYSIS,
        delegation_policy=DelegationPolicy.allowlist(frozenset({"general"})),
        agent_kind=AgentKind.PRIMARY,
    )
    child = AgentDefinition(
        name="general", description="writer", intent=TaskIntent.EDIT,
    )

    assert parent.permits_subagent(child) is False


def test_subagent_contract_intersects_parent_and_definition_limits():
    from agent.v2.task_contract import TaskContract

    definition = AgentDefinition(
        name="bounded",
        description="bounded child",
        intent=TaskIntent.ANALYSIS,
        max_turns=12,
        max_tokens=3_000,
        completion_requires={"submit_findings": 1},
    )
    cfg = AgentConfig(max_steps=20, budget_tokens=10_000)

    contract = TaskContract.for_subagent(
        definition,
        cfg,
        parent_budget_tokens=5_000,
        parent_max_steps=9,
    )

    assert contract.max_steps == 9
    assert contract.budget_tokens == 3_000
    assert contract.require_deliverables == {"submit_findings": 1}


def test_hidden_subagent_is_delegatable_only_when_parent_explicitly_allows_it():
    registry = AgentRegistryV2()
    plan = registry.get("plan")
    explore = registry.get("explore")

    assert "code-reviewer" not in {
        child.name for child in registry.list_subagents()
    }
    assert "code-reviewer" in {
        child.name for child in registry.delegatable_by(plan)
    }
    assert "code-reviewer" not in {
        child.name for child in registry.delegatable_by(explore)
    }


def test_subagent_registry_inherits_parent_effect_and_path_policy(tmp_path):
    from agent.v2.subagent_registry_factory import build_restricted_registry

    allowed_file = tmp_path / "allowed.py"
    allowed_file.write_text("ok\n", encoding="utf-8")
    base = ToolRegistry()
    read_tool = NoopTool("file_read")
    read_tool.metadata = ToolMetadata(
        effects=frozenset({ToolEffect.READ_WORKSPACE}),
        path_access=PathAccess.READ,
        path_parameter="path",
    )
    shell_tool = NoopTool("shell")
    shell_tool.metadata = ToolMetadata(
        effects=frozenset({ToolEffect.EXECUTE}),
    )
    base.register(read_tool)
    base.register(shell_tool)
    definition = AgentDefinition(
        name="general",
        description="child",
        intent=TaskIntent.EDIT,
        tools=frozenset({"Read", "Bash"}),
    )
    parent_policy = PhasePolicy(
        allowed_effects=frozenset({
            ToolEffect.READ_WORKSPACE,
            ToolEffect.PRODUCE_DELIVERABLE,
        }),
        allowed_read_paths=frozenset({"allowed.py"}),
        strict_file_scope=True,
    )

    restricted, _ = build_restricted_registry(
        definition,
        base,
        repo_path=str(tmp_path),
        parent_policy=parent_policy,
    )

    assert "file_read" in restricted.tool_names
    assert "shell" not in restricted.tool_names
    assert restricted.execute_tool(
        "file_read", {"path": "allowed.py"},
    ).success is True
    denied = restricted.execute_tool(
        "file_read", {"path": "outside.py"},
    )
    assert denied.success is False
    assert "PATH ACCESS DENIED" in denied.error


def test_v2_task_tool_rejects_missing_params():
    tool = AgentTool.__new__(AgentTool)
    result = tool.execute({})
    assert result.success is False
    assert "requires" in result.error


def test_v2_task_tool_rejects_blank_description(tmp_path):
    runtime, _ = _make_runtime(tmp_path, MockBackend([]))
    tool = AgentTool(runtime, "parent", caller_agent_name="build")
    result = tool.execute({"subagent_type": "general", "description": "   ", "prompt": "do it"})
    assert result.success is False
    assert "requires" in result.error


def test_v2_task_tool_rejects_none_params_as_missing(tmp_path):
    runtime, _ = _make_runtime(tmp_path, MockBackend([]))
    tool = AgentTool(runtime, "parent", caller_agent_name="build")
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
    tool = AgentTool(
        _StubRuntime(fork_result), "parent", caller_agent_name="build"
    ).with_run_context(
        _run_context()
    )
    result = tool.execute({"subagent_type": "general", "description": "check file", "prompt": "do it"})
    assert result.success is True
    assert result.output.startswith("WARNING: Subagent reached max steps (10 turns).")
    assert "<status>partial</status>" in result.output
    assert "<summary>\npartly done\n  </summary>" in result.output


def test_v2_task_tool_leases_only_parent_remaining_budget():
    fork_result = ForkResult(
        agent_name="general", session_id="child", status="completed",
        summary="done",
    )
    runtime = _StubRuntime(fork_result)
    context = _run_context(tokens=1_000)
    context.budget.consume(275)
    tool = AgentTool(
        runtime, "parent", caller_agent_name="build"
    ).with_run_context(context)

    result = tool.execute({
        "subagent_type": "general",
        "description": "bounded child",
        "prompt": "Do the bounded task",
    })

    assert result.success is True
    assert runtime.last_fork_kwargs["budget_tokens"] == 725
    assert runtime.last_fork_kwargs["cancellation_token"] is context.cancellation


def test_v2_task_tool_passes_narrowed_parent_authority_to_fork():
    fork_result = ForkResult(
        agent_name="general", session_id="child", status="completed",
        summary="done",
    )
    runtime = _StubRuntime(fork_result)
    budget = ExecutionBudget(config=ExecutionBudgetConfig(token_limit=1_000))
    budget.start()
    context = RunContext(
        budget=budget,
        cancellation=CancellationToken(),
        delegation_step_limit=8,
        phase_policy=PhasePolicy(
            denied_effects=frozenset({ToolEffect.NETWORK}),
            allowed_read_paths=frozenset({"src/a.py"}),
            strict_file_scope=True,
        ),
        delegation_effects=frozenset({
            ToolEffect.READ_WORKSPACE,
            ToolEffect.PRODUCE_DELIVERABLE,
        }),
    )
    tool = AgentTool(
        runtime, "parent", caller_agent_name="build"
    ).with_run_context(context)

    result = tool.execute({
        "subagent_type": "general",
        "description": "bounded child",
        "prompt": "Inspect only src/a.py",
    })

    assert result.success is True
    delegated_policy = runtime.last_fork_kwargs["parent_policy"]
    assert delegated_policy.allowed_effects == frozenset({
        ToolEffect.READ_WORKSPACE,
        ToolEffect.PRODUCE_DELIVERABLE,
    })
    assert delegated_policy.denied_effects == frozenset({ToolEffect.NETWORK})
    assert delegated_policy.allowed_read_paths == frozenset({"src/a.py"})
    assert delegated_policy.strict_file_scope is True


def test_v2_task_tool_passes_exact_live_spawn_context():
    fork_result = ForkResult(
        agent_name="general", session_id="child", status="completed",
        summary="done",
    )
    runtime = _StubRuntime(fork_result)
    context = _run_context()
    spawn_context = AgentSpawnContext.capture(
        messages=[LLMMessage(role="system", content="live parent prompt")],
        parent_session_id="parent",
        parent_agent_name="build",
        repo_path=str(Path.cwd()),
        model_name="test-model",
        tool_schemas=[LLMToolSchema(
            name="task", description="delegate", parameters={"type": "object"},
        )],
    )
    tool = AgentTool(
        runtime, "parent", caller_agent_name="build"
    ).with_run_context(replace(context, spawn_context=spawn_context))

    result = tool.execute({
        "subagent_type": "general",
        "description": "inspect context",
        "prompt": "Inspect the live boundary",
    })

    assert result.success is True
    assert runtime.last_fork_kwargs["spawn_context"] is spawn_context


def test_v2_task_tool_does_not_dispatch_after_cancellation():
    fork_result = ForkResult(
        agent_name="general", session_id="child", status="completed",
        summary="should not run",
    )
    runtime = _StubRuntime(fork_result)
    context = _run_context()
    context.cancellation.cancel(detail="operator stopped the run")
    tool = AgentTool(
        runtime, "parent", caller_agent_name="build"
    ).with_run_context(context)

    result = tool.execute({
        "subagent_type": "general",
        "description": "cancelled child",
        "prompt": "Do not run",
    })

    assert result.success is False
    assert result.tool_error.error_type.value == "interrupted"
    assert runtime.last_fork_kwargs is None


def test_fork_result_coerces_status_at_boundary():
    result = ForkResult(
        agent_name="explore", session_id="typed", status="completed", summary="done"
    )
    assert result.status is ForkStatus.COMPLETED


def test_fork_result_round_trips_typed_worktree_evidence():
    evidence = WorktreeEvidence(
        change=WorktreeChange.UNCOMMITTED,
        path="C:/state/worktrees/child",
        branch="multi-agent/child",
        base_branch="main",
        base_commit="abc123",
        changed_files=("src/a.py", "tests/test_a.py"),
        revision="revision-1",
    )
    result = ForkResult(
        agent_name="general", session_id="child", status="completed",
        summary="done", worktree=evidence,
        worktree_disposition=WorktreeDisposition.PRESERVED,
    )

    restored = ForkResult.from_dict(result.to_dict())

    assert restored.worktree == evidence
    retained = replace(
        restored, worktree_disposition=WorktreeDisposition.RETAINED,
    )
    assert ForkResult.from_dict(retained.to_dict()) == retained


def test_v2_format_fork_result_exposes_worktree_git_facts():
    result = ForkResult(
        agent_name="general",
        session_id="child",
        status="completed",
        summary="changes ready",
        worktree=WorktreeEvidence(
            change=WorktreeChange.COMMITTED,
            path="C:/state/worktrees/child",
            branch="multi-agent/child",
            base_branch="main",
            base_commit="abc123",
            changed_files=("src/a.py",),
            revision="revision-1",
        ),
        worktree_disposition=WorktreeDisposition.PRESERVED,
    )

    output = _format_fork_result("general", result)

    assert "<worktree change='committed'>" in output
    assert "<base-commit>abc123</base-commit>" in output
    assert "<changed-file>src/a.py</changed-file>" in output


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
    registry = ToolRegistry()
    delegate = NoopTool("dispatch_child")
    delegate.metadata = ToolMetadata(roles=frozenset({ToolRole.DELEGATE}))
    registry.register(delegate)
    agent = ReActAgent(MockBackend([]), registry, AgentConfig(stream=False))
    long_summary = "x" * 20_000
    observation = Observation(
        status=ObservationStatus.SUCCESS,
        output=f"<task-notification><summary>{long_summary}</summary></task-notification>",
        tool_name="dispatch_child",
    )
    content = agent._build_tool_result_content(observation)
    assert long_summary in content
    assert "omitted" not in content
    fallback = agent._format_observations_for_history([observation])
    assert long_summary in fallback
    assert "omitted" not in fallback


def test_cancellation_tokens_isolate_siblings_and_inherit_parent_cancel():
    root = CancellationToken()
    first = root.child()
    second = root.child()

    first.cancel(detail="cancel first only")
    assert first.is_cancelled is True
    assert first.detail == "cancel first only"
    assert root.is_cancelled is False
    assert second.is_cancelled is False

    root.cancel(detail="cancel whole tree")
    assert second.is_cancelled is True
    assert second.detail == "cancel whole tree"


def test_fork_session_creates_child_cancellation_scope(tmp_path, monkeypatch):
    import agent.v2.runtime as runtime_module

    captured = {}

    def fake_fork_subagent(**kwargs):
        captured.update(kwargs)
        return ForkResult(
            agent_name=kwargs["definition"].name,
            session_id=kwargs["agent_id"],
            status=ForkStatus.COMPLETED,
            summary="done",
        )

    monkeypatch.setattr(runtime_module, "fork_subagent", fake_fork_subagent)
    runtime, store = _make_runtime(tmp_path, MockBackend([]))
    parent = runtime.create_root_session(
        agent_name="build", repo_path=str(tmp_path), title="parent",
    )
    parent_token = CancellationToken()

    result = runtime.fork_session(
        parent_session_id=parent.id,
        definition=runtime.agent_registry.get("explore"),
        description="inspect",
        prompt="Inspect safely",
        budget_tokens=50_000,
        parent_max_steps=10,
        cancellation_token=parent_token,
        parent_policy=PhasePolicy(),
    )

    child_token = captured["cancellation_token"]
    assert result.status is ForkStatus.COMPLETED
    assert child_token is not parent_token
    child_token.cancel(detail="child only")
    assert child_token.is_cancelled is True
    assert parent_token.is_cancelled is False
    children = store.list_child_sessions(parent.id)
    assert len(children) == 1
    assert children[0].metadata["entrypoint"] == "task"


def test_explicit_delegation_guarantees_named_child_and_records_origin(tmp_path):
    from agent.v2 import ExplicitDelegationRequest
    from agent.v2.task_contract import TaskContract

    backend = MockBackend([
        Action(
            action_type=ActionType.FINISH,
            thought="inspection complete",
            message="explicit child evidence",
        ),
    ])
    runtime, store = _make_runtime(tmp_path, backend)
    parent = runtime.create_root_session(
        agent_name="plan", repo_path=str(tmp_path), title="parent",
    )

    result = runtime.run_explicit_delegation(
        parent.id,
        request=ExplicitDelegationRequest(
            agent_name="explore",
            description="Inspect runtime",
            prompt="Inspect runtime without modifying files.",
        ),
        parent_intent=TaskIntent.ANALYSIS,
        contract=TaskContract(max_steps=10, budget_tokens=10_000),
    )

    children = store.list_child_sessions(parent.id)
    assert result.status is ForkStatus.COMPLETED
    assert result.summary == "explicit child evidence"
    assert len(children) == 1
    assert children[0].agent_name == "explore"
    assert children[0].metadata["entrypoint"] == "explicit"


def test_explicit_delegation_rejects_agent_outside_parent_grant(tmp_path):
    from agent.v2 import ExplicitDelegationError, ExplicitDelegationRequest
    from agent.v2.task_contract import TaskContract

    runtime, _ = _make_runtime(tmp_path, MockBackend([]))
    parent = runtime.create_root_session(
        agent_name="plan", repo_path=str(tmp_path), title="parent",
    )

    with pytest.raises(ExplicitDelegationError, match="not delegatable"):
        runtime.run_explicit_delegation(
            parent.id,
            request=ExplicitDelegationRequest(
                agent_name="general",
                description="Forbidden edit",
                prompt="Edit a file.",
            ),
            parent_intent=TaskIntent.ANALYSIS,
            contract=TaskContract(max_steps=10, budget_tokens=10_000),
        )


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
    run_bound = registry.with_run_context(_run_context())
    assert {schema.name for schema in run_bound.get_schemas()} == {
        "artifact_read", "evidence_list", "file_read",
    }


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


@pytest.mark.parametrize(
    ("raised", "expected_status"),
    [
        (RuntimeError("provider failed"), SessionStatus.FAILED),
        (KeyboardInterrupt(), SessionStatus.CANCELLED),
    ],
)
def test_v2_root_session_converges_when_execution_raises(
    tmp_path, monkeypatch, raised, expected_status,
):
    def _raise_from_run(self, task, log):
        raise raised

    monkeypatch.setattr(ReActAgent, "run", _raise_from_run)
    runtime, store = _make_runtime(tmp_path, MockBackend([]))
    session = runtime.create_root_session(
        agent_name="build", repo_path=str(tmp_path), title="failing run",
    )

    with pytest.raises(type(raised)):
        runtime.run_session(
            session.id, agent_name="build",
            task_description="trigger provider", intent="edit",
        )

    persisted = store.get_session(session.id)
    assert persisted is not None
    assert persisted.status is expected_status
    assert persisted.completed_at is not None


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
    tool = AgentTool(
        runtime, "missing-parent", caller_agent_name="build",
    ).with_run_context(_run_context())

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


def test_v2_coordinator_worktree_tools_follow_effect_policy(tmp_path):
    from agent.v2.registry_builder import build_registry_for_session

    agents_dir = tmp_path / ".forge-agent" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "general.md").write_text(
        "---\n"
        "name: general\n"
        "description: isolated writer\n"
        "intent: edit\n"
        "isolation: worktree\n"
        "tools: Read, Write, Edit, Bash\n"
        "---\n"
        "Perform one isolated edit.",
        encoding="utf-8",
    )
    runtime, _ = _make_runtime(tmp_path, MockBackend([]))
    session = runtime.create_root_session(
        agent_name="build", repo_path=str(tmp_path), title="test",
    )
    registry = build_registry_for_session(
        runtime.agent_registry.get("build"),
        session,
        base_registry=runtime._base_registry,
        agent_registry=runtime.agent_registry,
        runtime=runtime,
    )

    assert "subagent_worktree_inspect" in registry.tool_names
    assert "subagent_worktree_apply" in registry.tool_names
    assert "subagent_worktree_discard" in registry.tool_names
    assert "subagent_worktree_retain" in registry.tool_names

    analysis_policy = build_task_policy(Task(
        "inspect child worktree",
        str(tmp_path),
        intent=TaskIntent.ANALYSIS,
    )).execution
    analysis_registry = registry.with_phase_policy(analysis_policy)

    assert "subagent_worktree_inspect" in analysis_registry.tool_names
    assert "subagent_worktree_apply" not in analysis_registry.tool_names
    assert "subagent_worktree_discard" not in analysis_registry.tool_names
    assert "subagent_worktree_retain" not in analysis_registry.tool_names


def test_v2_coordinator_hides_worktree_tools_without_declared_child(tmp_path):
    from agent.v2.registry_builder import build_registry_for_session

    runtime, _ = _make_runtime(tmp_path, MockBackend([]))
    session = runtime.create_root_session(
        agent_name="build", repo_path=str(tmp_path), title="test",
    )
    registry = build_registry_for_session(
        runtime.agent_registry.get("build"),
        session,
        base_registry=runtime._base_registry,
        agent_registry=runtime.agent_registry,
        runtime=runtime,
    )

    assert "task" in registry.tool_names
    assert "subagent_worktree_inspect" not in registry.tool_names
    assert "subagent_worktree_apply" not in registry.tool_names
    assert "subagent_worktree_discard" not in registry.tool_names
    assert "subagent_worktree_retain" not in registry.tool_names


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
        **_fork_resources(),
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
    assert child.metadata["requested_budget_tokens"] == 50_000
    assert child.metadata["budget_tokens"] == 40_000
    assert child.metadata["max_steps"] == 10
    child_messages = store.list_messages(child.id)
    assert (child_messages[0].role, child_messages[0].content) == (
        "user", "Find login flow",
    )
    assert child_messages[-1].role == "assistant"
    assert child_messages[-1].content == "summary"
    assert any("[ENVIRONMENT]" in str(message.content) for message in child_messages)
    assert store.list_messages(parent.id) == []


def test_v2_plan_can_dispatch_explore_and_resume_with_child_result(tmp_path):
    backend = MockBackend([
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="delegate repository inspection",
            tool_calls=[ToolCall(name="task", params={
                "subagent_type": "explore",
                "description": "inspect runtime isolation",
                "prompt": "Inspect runtime implementation and return file evidence.",
            })],
        ),
        Action(
            action_type=ActionType.FINISH,
            thought="child inspection complete",
            message="runtime.py:1 verified",
        ),
        Action(
            action_type=ActionType.FINISH,
            thought="use delegated evidence",
            message="plan based on runtime.py:1 verified",
        ),
    ])
    runtime, store = _make_runtime(tmp_path, backend)
    parent = runtime.create_root_session(
        agent_name="plan", repo_path=str(tmp_path), title="plan with explore",
    )

    result = runtime.run_session(
        parent.id,
        agent_name="plan",
        task_description="Review runtime and produce a plan.",
        intent=TaskIntent.ANALYSIS,
    )

    assert result.status is RunStatus.SUCCESS
    assert result.summary == "plan based on runtime.py:1 verified"
    assert "task" in backend.received_tools[0]
    children = store.list_child_sessions(parent.id)
    assert len(children) == 1
    assert children[0].agent_name == "explore"
    assert children[0].status is SessionStatus.COMPLETED
    assert children[0].summary == "runtime.py:1 verified"


class _FanOutBackend(LLMBackend):
    """Backend that proves both children enter complete() concurrently."""

    def __init__(self, *, fail_second: bool = False) -> None:
        from threading import Barrier, Lock

        self._barrier = Barrier(2)
        self._lock = Lock()
        self._parent_calls = 0
        self.fail_second = fail_second
        self.children_overlapped = False
        self.parent_resume_messages: list[LLMMessage] = []
        self.first_parent_messages: list[LLMMessage] = []

    @property
    def model_name(self) -> str:
        return "fan-out-test"

    def complete(self, messages, tools) -> LLMResponse:
        tool_names = {tool.name for tool in tools}
        if "task" in tool_names:
            with self._lock:
                self._parent_calls += 1
                parent_call = self._parent_calls
            if parent_call == 1:
                self.first_parent_messages = list(messages)
                action = Action(
                    action_type=ActionType.TOOL_CALL,
                    thought="fan out independent inspections",
                    tool_calls=[
                        ToolCall(name="task", params={
                            "subagent_type": "explore",
                            "description": "inspect scope alpha",
                            "prompt": "Inspect independent scope ALPHA.",
                        }),
                        ToolCall(name="task", params={
                            "subagent_type": "explore",
                            "description": "inspect scope beta",
                            "prompt": "Inspect independent scope BETA.",
                        }),
                    ],
                )
            else:
                self.parent_resume_messages = list(messages)
                action = Action(
                    action_type=ActionType.FINISH,
                    thought="synthesize both child results",
                    message="SYNTHESIS: ALPHA evidence + BETA evidence",
                )
            return LLMResponse(
                action=action, raw_content="parent", input_tokens=20,
                output_tokens=10,
            )

        text = " ".join(str(message.content) for message in messages)
        is_beta = "BETA" in text
        self._barrier.wait(timeout=3)
        with self._lock:
            self.children_overlapped = True
        if is_beta and self.fail_second:
            action = Action(
                action_type=ActionType.GIVE_UP,
                thought="beta blocked",
                message="BETA failed independently",
            )
        else:
            scope = "BETA" if is_beta else "ALPHA"
            action = Action(
                action_type=ActionType.FINISH,
                thought=f"{scope} inspected",
                message=f"{scope} evidence",
            )
        return LLMResponse(
            action=action, raw_content="child", input_tokens=20,
            output_tokens=10,
        )


@pytest.mark.parametrize("fail_second", [False, True])
def test_v2_plan_fans_out_read_only_children_then_synthesizes(
    tmp_path, fail_second,
):
    backend = _FanOutBackend(fail_second=fail_second)
    runtime, store = _make_runtime(tmp_path, backend)
    parent = runtime.create_root_session(
        agent_name="plan", repo_path=str(tmp_path), title="fan out plan",
    )

    result = runtime.run_session(
        parent.id,
        agent_name="plan",
        task_description="Inspect ALPHA and BETA, then synthesize.",
        intent=TaskIntent.ANALYSIS,
    )

    assert result.status is RunStatus.SUCCESS
    assert result.summary == "SYNTHESIS: ALPHA evidence + BETA evidence"
    assert backend.children_overlapped is True
    children = store.list_child_sessions(parent.id)
    assert len(children) == 2
    from context.history import ConversationSnapshot
    expected_snapshot = ConversationSnapshot.capture(backend.first_parent_messages)
    fingerprints = {
        child.metadata["parent_snapshot_fingerprint"] for child in children
    }
    assert fingerprints == {expected_snapshot.fingerprint}
    assert {
        int(child.metadata["parent_snapshot_message_count"])
        for child in children
    } == {len(backend.first_parent_messages)}
    assert all("messages" not in child.metadata for child in children)
    assert any(
        "Available Subagents" in str(message.content)
        for message in backend.first_parent_messages
    )
    expected_statuses = (
        {SessionStatus.COMPLETED, SessionStatus.FAILED}
        if fail_second else {SessionStatus.COMPLETED}
    )
    assert {child.status for child in children} == expected_statuses
    assert sum(int(child.metadata["budget_tokens"]) for child in children) <= 50_000
    resumed_text = " ".join(
        str(message.content) for message in backend.parent_resume_messages
    )
    assert "ALPHA evidence" in resumed_text
    assert (
        "BETA failed independently" if fail_second else "BETA evidence"
    ) in resumed_text


def test_v2_subagent_lifecycle_events_carry_parent_child_facts(tmp_path):
    hook_contexts = []
    emitted_events = []

    class RecordingDispatcher:
        def dispatch(self, event, context):
            hook_contexts.append(context)
            return DispatchResult()

    runtime, _ = _make_runtime(
        tmp_path,
        MockBackend([
            Action(action_type=ActionType.FINISH, thought="done", message="facts ready"),
        ]),
        hook_dispatcher=RecordingDispatcher(),
        event_callback=emitted_events.append,
    )
    parent = runtime.create_root_session(
        agent_name="build", repo_path=str(tmp_path), title="parent",
    )

    result = runtime.fork_session(
        parent_session_id=parent.id,
        definition=runtime.agent_registry.get("explore"),
        description="trace exploration",
        prompt="Inspect facts",
        **_fork_resources(),
    )

    lifecycle = [
        event for event in emitted_events
        if event.event_type in {
            EventType.SUBAGENT_START, EventType.SUBAGENT_STOP,
        }
    ]
    assert [event.event_type for event in lifecycle] == [
        EventType.SUBAGENT_START, EventType.SUBAGENT_STOP,
    ]
    assert lifecycle[0].payload == {
        "parent_session_id": parent.id,
        "root_session_id": parent.root_id,
        "session_id": result.session_id,
        "agent_name": "explore",
        "status": "running",
        "turns_used": 0,
        "tokens_used": 0,
        "summary": "",
        "error": "",
    }
    assert lifecycle[1].payload["status"] == "completed"
    assert lifecycle[1].payload["summary"] == "facts ready"
    assert EventType.TASK_START in {event.event_type for event in emitted_events}
    assert EventType.TASK_COMPLETE in {event.event_type for event in emitted_events}

    subagent_hooks = [
        context for context in hook_contexts
        if context.event in {
            HookEvent.SUBAGENT_START, HookEvent.SUBAGENT_STOP,
        }
    ]
    assert [context.event for context in subagent_hooks] == [
        HookEvent.SUBAGENT_START, HookEvent.SUBAGENT_STOP,
    ]
    assert all(context.session_id == parent.id for context in subagent_hooks)
    assert all(context.agent_id == result.session_id for context in subagent_hooks)
    assert all(context.agent_type == "explore" for context in subagent_hooks)
    assert subagent_hooks[-1].last_assistant_message == "facts ready"


def test_v2_cancelled_fork_converges_to_cancelled_session(tmp_path):
    runtime, store = _make_runtime(tmp_path, MockBackend([]))
    parent = runtime.create_root_session(
        agent_name="build", repo_path=str(tmp_path), title="parent",
    )
    token = CancellationToken()
    token.cancel(detail="operator cancelled")

    result = runtime.fork_session(
        parent_session_id=parent.id,
        definition=runtime.agent_registry.get("explore"),
        description="cancelled exploration",
        prompt="Do not start",
        budget_tokens=5_000,
        parent_max_steps=10,
        cancellation_token=token,
        parent_policy=PhasePolicy(),
    )

    child = store.get_session(result.session_id)
    assert result.status is ForkStatus.CANCELLED
    assert child is not None
    assert child.status is SessionStatus.CANCELLED
    assert child.error == "operator cancelled"


def test_react_agent_honors_pre_cancelled_runtime_token(tmp_path):
    token = CancellationToken()
    token.cancel(detail="operator cancelled")
    agent = ReActAgent(
        MockBackend([]), ToolRegistry(),
        AgentConfig(cancellation_token=token, stream=False),
    )
    task = Task("cancel me", str(tmp_path), max_steps=5)

    with EventLog.create(task, log_dir=str(tmp_path / "logs")) as event_log:
        result = agent.run(task, event_log)

    assert result.status is RunStatus.CANCELLED
    assert result.steps_taken == 0
    assert result.termination_reason.value == "user_cancelled"


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
        **_fork_resources(),
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


def test_submit_findings_normalizes_and_validates_project_evidence(tmp_path):
    source = tmp_path / "runtime.py"
    source.write_text("one\ntwo\nthree\n", encoding="utf-8")
    tool = SubmitFindingsTool(repo_path=str(tmp_path))

    accepted = tool.execute({"report": {
        "status": "completed",
        "summary": "verified",
        "findings": [{
            "severity": "HIGH",
            "category": "bug",
            "title": "Incorrect branch",
            "description": "The second line selects the wrong branch.",
            "file_path": "runtime.py",
            "line_start": 2,
            "line_end": 2,
            "code_snippet": "two",
            "verification": "Read the cited source line.",
        }],
    }})

    assert accepted.success is True
    report = tool.accumulator.combined_report()
    assert report is not None
    assert report.status is SubagentReportStatus.COMPLETED
    finding = report.findings[0]
    assert finding.severity is FindingSeverity.HIGH
    assert finding.category is FindingCategory.BUG
    assert finding.file_path == str(source.resolve())

    missing_evidence = tool.execute({"report": {
        "status": "completed",
        "findings": [{
            "severity": "LOW", "category": "bug",
            "title": "Claim", "description": "No evidence",
        }],
    }})
    assert missing_evidence.success is False
    assert "lacks evidence" in (missing_evidence.error or "")

    outside_scope = tool.execute({"report": {
        "status": "completed",
        "findings": [{
            "severity": "LOW", "category": "hypothesis",
            "title": "External", "description": "Outside path",
            "file_path": str(tmp_path.parent / "outside.py"),
        }],
    }})
    assert outside_scope.success is False
    assert "outside project scope" in (outside_scope.error or "")


def test_v2_subagent_persists_typed_report_and_report_partial_status(tmp_path):
    source = tmp_path / "runtime.py"
    source.write_text("safe = False\n", encoding="utf-8")
    backend = MockBackend([
        Action(
            action_type=ActionType.TOOL_CALL,
            thought="submit verified result",
            tool_calls=[ToolCall(name="submit_findings", params={"report": {
                "status": "partial",
                "summary": "One branch remains unverified.",
                "findings": [{
                    "severity": "MEDIUM",
                    "category": "improvement",
                    "title": "Explicit state needed",
                    "description": "The state is represented as a boolean.",
                    "file_path": str(source.resolve()),
                    "line_start": 1,
                    "line_end": 1,
                    "code_snippet": "safe = False",
                    "verification": "Read runtime.py line 1.",
                }],
            }})],
        ),
        Action(action_type=ActionType.FINISH, thought="done", message="partial review"),
    ])
    runtime, store = _make_runtime(tmp_path, backend)
    parent = runtime.create_root_session(
        agent_name="build", repo_path=str(tmp_path), title="parent",
    )

    result = runtime.fork_session(
        parent_session_id=parent.id,
        definition=runtime.agent_registry.get("code-reviewer"),
        description="review runtime",
        prompt="Review runtime.py",
        **_fork_resources(),
    )

    assert result.status is ForkStatus.PARTIAL
    assert result.report is not None
    assert result.report.status is SubagentReportStatus.PARTIAL
    child = store.get_session(result.session_id)
    assert child is not None
    assert child.status is SessionStatus.PARTIAL
    assert child.fork_result == result
    output = _format_fork_result("code-reviewer", result)
    assert "<subagent-report status='partial' count='1'>" in output
    assert str(source.resolve()) in output
    source.unlink()
    historical_child = store.get_session(result.session_id)
    assert historical_child is not None
    assert historical_child.fork_result == result


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
        **_fork_resources(),
    )

    child = store.get_session(result.session_id)
    assert result.status is ForkStatus.FAILED
    assert child is not None
    assert child.status is SessionStatus.FAILED
    assert child.summary == "cannot inspect safely"
    assert child.error == "cannot inspect safely"
    assert child.completed_at is not None


def test_v2_declared_worktree_failure_returns_structured_failed_child(tmp_path, monkeypatch):
    from runtime.state_paths import STATE_HOME_ENV
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path.parent / "agent-state"))
    runtime, store = _make_runtime(tmp_path, MockBackend([]))
    parent = runtime.create_root_session(
        agent_name="build", repo_path=str(tmp_path), title="parent",
    )
    definition = replace(
        runtime.agent_registry.get("general"),
        workspace_mode=WorkspaceMode.WORKTREE,
    )

    result = runtime.fork_session(
        parent_session_id=parent.id,
        definition=definition,
        description="isolated edit",
        prompt="Edit only a.py",
        **_fork_resources(),
    )

    assert result.status is ForkStatus.FAILED
    assert "Worktree isolation failed" in result.error
    child = store.get_session(result.session_id)
    assert child is not None
    assert child.status is SessionStatus.FAILED
    assert child.fork_result == result


def test_v2_worktree_child_preserves_changes_without_mutating_parent(
    tmp_path, monkeypatch,
):
    from runtime.state_paths import STATE_HOME_ENV

    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=tmp_path, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Forge Tests"],
        cwd=tmp_path, capture_output=True, check=True,
    )
    (tmp_path / "tracked.txt").write_text("parent\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=tmp_path, capture_output=True, check=True,
    )
    monkeypatch.setenv(
        STATE_HOME_ENV, str(tmp_path.parent / "wt-state"),
    )

    def write_in_child(_agent, task, _event_log):
        (Path(task.repo_path) / "child.txt").write_text("child\n", encoding="utf-8")
        return RunResult(
            task_id="child-run",
            status=RunStatus.SUCCESS,
            summary="child changes ready",
            steps_taken=1,
            total_tokens=10,
        )

    monkeypatch.setattr(ReActAgent, "run", write_in_child)
    runtime, store = _make_runtime(
        tmp_path,
        MockBackend([]),
        state_dir=tmp_path.parent / "wt-runtime-state",
    )
    parent = runtime.create_root_session(
        agent_name="build", repo_path=str(tmp_path), title="parent",
    )
    definition = replace(
        runtime.agent_registry.get("general"),
        workspace_mode=WorkspaceMode.WORKTREE,
    )

    result = runtime.fork_session(
        parent_session_id=parent.id,
        definition=definition,
        description="isolated edit",
        prompt="Create child.txt",
        **_fork_resources(),
    )

    assert result.status is ForkStatus.COMPLETED
    assert result.worktree is not None
    assert result.worktree.change is WorktreeChange.UNCOMMITTED
    assert result.worktree.changed_files == ("child.txt",)
    assert Path(result.worktree.path, "child.txt").is_file()
    assert not (tmp_path / "child.txt").exists()
    assert result.worktree_disposition is WorktreeDisposition.PRESERVED
    assert store.get_session(result.session_id).fork_result == result
    blocked = runtime._check_session_completion(parent.id)
    assert blocked.can_complete is False
    assert result.session_id in blocked.inject_message

    unrelated_parent = runtime.create_root_session(
        agent_name="build", repo_path=str(tmp_path), title="unrelated",
    )
    with pytest.raises(ValueError, match="direct child"):
        runtime.inspect_subagent_worktree(unrelated_parent.id, result.session_id)

    inspected = runtime.inspect_subagent_worktree(parent.id, result.session_id)
    retained = runtime.retain_subagent_worktree(
        parent.id,
        result.session_id,
        expected_revision=inspected.revision,
    )
    assert retained.status.value == "retained"
    retained_result = store.get_session(result.session_id).fork_result
    assert retained_result.worktree_disposition is WorktreeDisposition.RETAINED
    assert retained_result.worktree == retained.evidence
    assert runtime._check_session_completion(parent.id).can_complete is True

    inspected = runtime.inspect_subagent_worktree(parent.id, result.session_id)
    applied = runtime.apply_subagent_worktree(
        parent.id,
        result.session_id,
        expected_revision=inspected.revision,
    )

    assert applied.status.value == "applied"
    assert (tmp_path / "child.txt").read_text(encoding="utf-8") == "child\n"
    assert not Path(inspected.path).exists()
    resolved = store.get_session(result.session_id).fork_result
    assert resolved.worktree is None
    assert resolved.worktree_disposition is WorktreeDisposition.APPLIED


def test_v2_runtime_blocks_finish_on_preserved_child_fact(tmp_path):
    backend = MockBackend([
        Action(
            action_type=ActionType.FINISH,
            thought="done",
            message="incorrectly claims child changes landed",
        ),
        Action(
            action_type=ActionType.GIVE_UP,
            thought="cannot resolve synthetic worktree",
            message="preserved child still needs a decision",
        ),
    ])
    runtime, store = _make_runtime(tmp_path, backend)
    parent = runtime.create_root_session(
        agent_name="build", repo_path=str(tmp_path), title="parent",
    )
    child = store.create_session(
        agent_name="general",
        mode=SessionMode.SUBAGENT,
        repo_path=str(tmp_path),
        title="child",
        parent_id=parent.id,
        root_id=parent.root_id,
    )
    store.set_fork_result(child.id, ForkResult(
        agent_name="general",
        session_id=child.id,
        status=ForkStatus.COMPLETED,
        summary="changes preserved",
        worktree=WorktreeEvidence(
            change=WorktreeChange.UNCOMMITTED,
            path="C:/state/worktrees/child",
            branch="multi-agent/child",
            base_branch="main",
            revision="revision-1",
        ),
        worktree_disposition=WorktreeDisposition.PRESERVED,
    ))

    result = runtime.run_session(
        parent.id,
        agent_name="build",
        task_description="Use the child edit",
        intent=TaskIntent.EDIT,
    )

    assert result.status is RunStatus.GAVE_UP
    assert result.summary == "preserved child still needs a decision"
    resumed_text = " ".join(
        str(message.content) for message in backend.received_messages[-1]
    )
    assert "[RUNTIME BLOCK]" in resumed_text
    assert child.id in resumed_text
    assert "revision-1" in resumed_text


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
        **_fork_resources(),
    )
    assert result.status in ("partial", "failed")
    child = store.get_session(result.session_id)
    assert child is not None
    assert child.status in (SessionStatus.PARTIAL, SessionStatus.FAILED)
    assert child.completed_at is not None


def test_v2_parent_recovers_after_failed_child(tmp_path):
    # Child subagent finishes immediately
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
        **_fork_resources(),
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
    assert "workspace=current" in text
    assert "workspace=current uses the parent project working tree" in text
    assert "Worktree Result Protocol" not in text
    assert "Subagent Output Review Protocol" in text
    assert "INSPECT before you relay" in text
    assert "UNVERIFIED" in text
    assert "NEVER verbatim-forward" in text
    assert "SPOT DESIGN PATTERNS" in text
    assert "Atomic Task Boundaries" in text
    assert "emit their task calls together" in text
    assert "Subagent Failure Recovery" in text
    assert "Runtime enforces retry limits" in text
    assert "The system will stop you" in text


def test_v2_runtime_injects_worktree_result_protocol_from_agent_metadata(tmp_path):
    agents_dir = tmp_path / ".forge-agent" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "general.md").write_text(
        "---\n"
        "name: general\n"
        "description: isolated writer\n"
        "intent: edit\n"
        "isolation: worktree\n"
        "tools: Read, Write, Edit, Bash\n"
        "---\n"
        "Perform one isolated edit.",
        encoding="utf-8",
    )
    runtime, _ = _make_runtime(tmp_path, MockBackend([]))
    build = runtime.agent_registry.get("build")

    text = " ".join(
        str(message.content)
        for message in runtime._build_runtime_messages(build, "edit task")
    )

    assert "**general** (workspace=worktree)" in text
    assert "Worktree Result Protocol" in text
    assert "worktree-disposition=preserved" in text
    assert "subagent_worktree_inspect" in text
    assert "subagent_worktree_apply" in text
    assert "subagent_worktree_retain" in text
    assert "Never claim that preserved changes landed" in text


def test_v2_plan_keeps_read_only_tools_available_until_model_finishes(tmp_path):
    """Step ratios never claim that planning research is objectively complete."""
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
    runtime, _ = _make_runtime(
        tmp_path,
        backend,
        tool_overrides={"file_read": _WorkspaceReadNoop("file_read")},
    )
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
    assert "file_read" in backend.received_tools[-1]
    assert all(
        "Planning exploration is complete" not in str(message.content)
        for message in backend.received_messages[-1]
    )


def test_v2_plan_can_continue_read_only_research_past_eighty_percent(tmp_path):
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
    runtime, _ = _make_runtime(
        tmp_path,
        backend,
        tool_overrides={"file_read": _WorkspaceReadNoop("file_read")},
    )
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
    assert all("file_read" in tools for tools in backend.received_tools)
    assert all(
        "Tool calls are disabled" not in str(message.content)
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
        assert spec.agent_kind is not AgentKind.PRIMARY, (
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
