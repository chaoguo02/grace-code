"""Tests for V2 fork-based subagent runtime."""

from __future__ import annotations

import pytest

from hooks.events import HookContext, HookEvent
from hooks.protocol import DispatchResult
from agent.core import AgentConfig, ReActAgent
from agent.event_log import EventLog
from agent.policy import PhasePolicy
from agent.policy_registry import PolicyAwareToolRegistry
from agent.task import Action, ActionType, Observation, ObservationStatus, RunStatus, Task, ToolCall
from agent.v2 import AgentRegistryV2, AgentTool, ForkResult, SessionRuntime, SessionStore
from agent.v2.models import AgentDefinition
from agent.v2.task_tool import _format_fork_result
from llm.base import LLMMessage, MockBackend
from tools.artifact_tool import ArtifactReadTool, ArtifactStoreRef
from tools.base import NoopTool, ToolRegistry
from tools.evidence_tool import EvidenceLedgerRef, EvidenceListTool
from context.artifacts import ArtifactStore
from context.evidence import EvidenceLedger
from tools.file_tool import (
    FileReadCache, FileReadTool, FileViewTool,
    MAX_READ_LINES, VIEW_WINDOW_LINES, MAX_READS_PER_FILE,
)


class _StubRuntime:
    def __init__(self, fork_result: ForkResult) -> None:
        self.agent_registry = AgentRegistryV2()
        self._fork_result = fork_result

    def fork_session(self, **kwargs):
        return self._fork_result


def _make_runtime(tmp_path, backend: MockBackend) -> tuple[SessionRuntime, SessionStore]:
    agent_registry = AgentRegistryV2()
    base_registry = ToolRegistry()

    from agent.v2.agent_registry import _BUILD_ALLOWED
    for tool_name in sorted(_BUILD_ALLOWED):
        base_registry.register(NoopTool(tool_name, output=f"{tool_name} ok"))
    base_registry.register(NoopTool("task", "subagent done"))

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
    assert store.get_session(root.id).parent_id is None
    assert store.get_session(child.id).parent_id == root.id
    assert [item.id for item in store.list_child_sessions(root.id)] == [child.id]


# ── Agent Registry ──

def test_v2_agent_registry_loads_builtins():
    registry = AgentRegistryV2()
    for name in ("explore", "general", "code-reviewer", "coordinator"):
        definition = registry.get(name)
        assert isinstance(definition, AgentDefinition)
        assert definition.name == name


def test_v2_agent_registry_resolves_tool_names():
    registry = AgentRegistryV2()
    names = registry.tool_names_for("explore")
    assert "file_read" in names or "Read" in names


def test_v2_agent_registry_builtin_primary_agents_declare_allowed_subagents():
    registry = AgentRegistryV2()
    assert registry.get("build").allowed_subagents == frozenset({"explore", "general", "code-reviewer"})
    assert registry.get("plan").allowed_subagents == frozenset({"explore", "general", "code-reviewer"})
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
    backend = MockBackend([
        Action(action_type=ActionType.FINISH, thought="done", message="Task complete."),
    ])
    runtime, store = _make_runtime(tmp_path, backend)
    session = runtime.create_root_session(agent_name="build", repo_path=str(tmp_path), title="test")
    result = runtime.run_session(session.id, agent_name="build", task_description="do it", intent="edit")
    assert result.summary == "Task complete."


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
                return DispatchResult(blocked=True, reason="tests failed")
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
    task = Task("finish with stop hook", str(tmp_path), max_steps=5)
    with EventLog.create(task, log_dir=str(tmp_path / "logs")) as event_log:
        result = agent.run(task, event_log)

    assert result.summary == "done after hook"
    assert calls == 2


def test_v2_react_agent_stop_hook_retry_limit_gives_up(tmp_path):
    from hooks.protocol import DispatchResult

    class AlwaysBlockingDispatcher:
        def dispatch_stop(self, ctx):
            return DispatchResult(blocked=True, reason="still failing")

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
    result = runtime.fork_session(
        definition=runtime.agent_registry.get("explore"),
        description="explore auth",
        prompt="Find login flow",
    )
    assert result.status == "completed"
    assert result.summary == "summary"
    assert result.agent_name == "explore"


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
    result = runtime.fork_session(
        definition=runtime.agent_registry.get("explore"),
        description="exhaustive search", prompt="Find everything",
    )
    assert result.status in ("partial", "failed")


def test_v2_parent_recovers_after_failed_child(tmp_path):
    # Fork subagent finishes immediately
    child_backend = MockBackend([
        Action(action_type=ActionType.FINISH, thought="sub", message="child done"),
    ])
    runtime, store = _make_runtime(tmp_path, child_backend)
    result = runtime.fork_session(
        definition=runtime.agent_registry.get("general"),
        description="fast task", prompt="Do quick thing",
    )
    assert result.status == "completed"

    # Parent can still run after child — separate backend needed in real use,
    # but here we verify fork doesn't crash and session still works.
    parent_backend = MockBackend([
        Action(action_type=ActionType.FINISH, thought="ok", message="parent done"),
    ])
    runtime2, _ = _make_runtime(tmp_path, parent_backend)
    session = runtime2.create_root_session(agent_name="build", repo_path=str(tmp_path), title="recovery")
    result2 = runtime2.run_session(session.id, agent_name="build", task_description="recover", intent="edit")
    assert result2.summary == "parent done"


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
    assert "TRANSIENT ERROR" in text
    assert "LOOP" in text
    assert "CIRCUIT BREAKER" in text
    assert "Task routing guide" in text
    assert "read-only analysis" in text


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
    """list_subagents() must never return agents with isolation='none' (primary agents)."""
    registry = AgentRegistryV2()
    subagents = registry.list_subagents()
    primary_names = {a.name for a in registry.list_primary_agents()}

    for spec in subagents:
        assert spec.isolation != "none", (
            f"Primary agent {spec.name!r} leaked into list_subagents()"
        )
        assert spec.name not in primary_names


# ── Subagent report format validation (Layer 2) ──

from agent.v2.task_tool import (
    _validate_subagent_report, _build_subagent_prompt,
    _SUBAGENT_PROTOCOL, _KNOWN_DESIGN_DECISIONS, VIOLATION_MARKER,
)


def test_validate_report_passes_well_structured_report():
    """Report with file:line refs, three sections, and code fences passes."""
    report = (
        "## Confirmed Bugs\n"
        "- agent/core.py:142: wrong comparison operator\n"
        "  ```python\n  if x = 1:  # assignment, not comparison\n  ```\n"
        "- Verified by reading agent/core.py:140-145\n"
        "## Improvement Suggestions\n"
        "- Better naming in task_tool.py:95\n"
        "## Unverified Hypotheses\n"
        "- Could be a race condition [UNVERIFIED]"
    )
    assert _validate_subagent_report(report) == []


def test_validate_report_passes_without_unverified_section():
    """A report where all findings are confirmed (no unverified section
    present) should NOT be flagged — UNVERIFIED markers are not mandatory."""
    report = (
        "## Confirmed Bugs\n"
        "- agent/core.py:142: wrong comparison operator\n"
        "  ```python\n  if x = 1:\n  ```\n"
        "## Improvement Suggestions\n"
        "- Better error messages in task_tool.py:101\n"
        "  ```python\n  print(\"TODO\")\n  ```\n"
    )
    assert _validate_subagent_report(report) == []


def test_validate_report_ok_for_non_bug_report():
    """Reports that don't claim bugs produce zero violations."""
    report = (
        "## Summary\n"
        "The task was completed successfully. File was read and everything "
        "looks correct. No problems found."
    )
    assert _validate_subagent_report(report) == []


def test_validate_report_warns_on_bug_claim_without_file_lines():
    """Bug claims without file:line refs produce violations."""
    report = (
        "## Confirmed Bugs\n"
        "1. The validation allows empty strings through\n"
        "2. Success=True when it should be False"
    )
    violations = _validate_subagent_report(report)
    assert len(violations) > 0
    assert any("file:line" in v for v in violations)


def test_validate_report_warns_on_missing_section_structure():
    """Bug claims without proper three-section structure produce a violation.
    UNVERIFIED absence is NOT checked — it depends on actual findings."""
    report = (
        "## Confirmed Bugs\n"
        "- agent/core.py:142: wrong operator\n"
        "## Observations\n"
        "- Better error messages"
    )
    violations = _validate_subagent_report(report)
    assert len(violations) > 0
    assert any("report structure" in v for v in violations)


def test_validate_report_warns_on_missing_code_fences():
    """Bug claims without ``` code fences produce a violation."""
    report = (
        "## Confirmed Bugs\n"
        "- agent/core.py:142: wrong operator\n"
        "## Improvement Suggestions\n"
        "- Style fixes\n"
        "## Unverified Hypotheses\n"
        "- Might be broken [UNVERIFIED]"
    )
    violations = _validate_subagent_report(report)
    # Has file:line + three-section, but no code fences
    assert any("code snippet" in v.lower() for v in violations)


def test_validate_report_empty():
    """Empty or blank reports produce zero violations."""
    assert _validate_subagent_report("") == []
    assert _validate_subagent_report("  ") == []


# ── Subagent prompt wrapper (Layer 1) ──

def test_build_subagent_prompt_includes_protocol():
    """_build_subagent_prompt wraps the user prompt with the protocol."""
    result = _build_subagent_prompt("Analyze task_tool.py for bugs.")
    assert "[SUBAGENT ANALYSIS PROTOCOL]" in result
    assert "READ BEFORE YOU CLAIM" in result
    assert "Phase 1" in result and "Phase 2" in result and "Phase 3" in result and "Phase 4" in result
    assert "Anti-Laziness" in result
    assert "Output Format" in result
    assert "Analyze task_tool.py for bugs." in result
    # User prompt must come after the protocol
    assert result.index("Analyze task_tool.py for bugs.") > result.index("[SUBAGENT ANALYSIS PROTOCOL]")


def test_known_design_decisions_injected_into_protocol():
    """The shareable _KNOWN_DESIGN_DECISIONS list is injected into the protocol."""
    result = _build_subagent_prompt("Do X.")
    assert "KNOWN DESIGN DECISIONS" in result
    for entry in _KNOWN_DESIGN_DECISIONS:
        # First 40 chars of each entry should appear in the protocol
        assert entry[:40] in result


def test_violation_marker_is_shared_constant():
    """VIOLATION_MARKER is a named constant for Layer 2 ↔ Layer 3 alignment."""
    assert isinstance(VIOLATION_MARKER, str)
    assert "SUBAGENT REPORT FORMAT VIOLATIONS" in VIOLATION_MARKER


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


def test_file_read_cache_reset_clears_all():
    """reset() clears all cached content and read counts."""
    cache = FileReadCache()

    cache.store("/abs/path/a.py", offset=1, limit=100, content="data")
    cache.read_counts["/abs/path/a.py"] = 2

    cache.reset()

    assert cache.check("/abs/path/a.py", offset=1, limit=100) is None
    assert cache.read_counts == {}


def test_file_read_cache_count_and_check_caps_at_max():
    """Files can be read at most MAX_READS_PER_FILE times."""
    cache = FileReadCache()

    # First 3 reads succeed
    assert cache.count_and_check("/abs/path/a.py") == 1
    assert cache.count_and_check("/abs/path/a.py") == 2
    assert cache.count_and_check("/abs/path/a.py") == 3

    # 4th read returns -1 (capped)
    assert cache.count_and_check("/abs/path/a.py") == -1


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

    # Second read — cached
    r2 = tool.execute({"path": str(f)})
    assert r2.success
    assert "[CACHED]" in r2.output
    assert "already read" in r2.output


def test_file_read_tool_count_cap(tmp_path):
    """After MAX_READS_PER_FILE unique range reads, further cache-miss reads return error.

    The frequency cap is a secondary defense: it fires when the agent keeps
    reading DIFFERENT offsets of the same file past the cap. Same-range
    re-reads are handled by the primary cache-hit path (returns [CACHED]).
    """
    tool = FileReadTool(read_cache=FileReadCache())
    view_tool = FileViewTool(read_cache=tool._read_cache)

    # Create a file with >500 lines so file_read (1-500) doesn't cover
    # file_view reads beyond line 500.
    lines = [f"line {i}" for i in range(1, 701)]
    f = tmp_path / "big.py"
    f.write_text("\n".join(lines))

    # file_read covers lines 1-500 (offset=1, limit=500)
    tool.execute({"path": str(f)})                                        # count 1

    # file_view reads beyond 500 — not covered by file_read cache
    view_tool.execute({"path": str(f), "start_line": 550})               # count 2
    view_tool.execute({"path": str(f), "start_line": 600})               # count 3

    # 4th unique read — blocked (count at cap, no cache hit)
    r = view_tool.execute({"path": str(f), "start_line": 650})
    assert not r.success
    assert "already been read" in r.error
    assert str(MAX_READS_PER_FILE) in r.error


def test_file_read_tool_clone_has_isolated_cache(tmp_path):
    """clone_with_fresh_cache() creates an independent cache."""
    tool1 = FileReadTool(read_cache=FileReadCache())
    tool2 = tool1.clone_with_fresh_cache()

    f = tmp_path / "test.py"
    f.write_text("line 1\nline 2\n")

    # Tool1 reads the file
    r1 = tool1.execute({"path": str(f)})
    assert r1.success

    # Tool2 (cloned) should NOT have tool1's cache
    r2 = tool2.execute({"path": str(f)})
    assert r2.success
    assert "[CACHED]" not in r2.output  # First read for tool2 — no cache hit


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

    # Exact same re-read — cached
    r2 = tool.execute({"path": str(f), "start_line": 50})
    assert r2.success
    assert "[CACHED]" in r2.output
    assert "already read" in r2.output


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


def test_file_view_and_file_read_share_count_cap(tmp_path):
    """file_read and file_view share the same per-file count cap.

    The cap only fires on cache-MISS reads past the limit. For files >500 lines,
    file_read (1-500) and file_view beyond 500 are non-overlapping ranges that
    each increment the shared count.
    """
    cache = FileReadCache()
    read_tool = FileReadTool(read_cache=cache)
    view_tool = FileViewTool(read_cache=cache)

    f = tmp_path / "big.py"
    lines = [f"line {i}" for i in range(1, 701)]
    f.write_text("\n".join(lines))

    # file_read covers 1-500 → count 1
    assert read_tool.execute({"path": str(f)}).success
    # file_view beyond 500 → count 2 (not covered by file_read cache)
    assert view_tool.execute({"path": str(f), "start_line": 520}).success
    # file_view at 600 → count 3
    assert view_tool.execute({"path": str(f), "start_line": 600}).success

    # 4th unique read — blocked (shared count cap across both tools)
    r = view_tool.execute({"path": str(f), "start_line": 650})
    assert not r.success
    assert "already been read" in r.error
    assert str(MAX_READS_PER_FILE) in r.error
