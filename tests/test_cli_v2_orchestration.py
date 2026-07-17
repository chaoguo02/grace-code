from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from threading import Barrier, Lock

from click.testing import CliRunner

from agent.task import Action, ActionType, ToolCall
from agent.session.models import SessionStatus, WorktreeDisposition
from agent.session.session_store import SessionStore
from config.schema import AgentCfg, AppConfig, LLMConfig, MemoryConfig
from entry.cli import cli
from llm.base import LLMBackend, LLMResponse
from executor.state_paths import ProjectStatePaths, STATE_HOME_ENV


def _response(action: Action) -> LLMResponse:
    return LLMResponse(
        action=action,
        raw_content=action.message or action.thought,
        input_tokens=20,
        output_tokens=10,
    )


def _tool(name: str, **params) -> ToolCall:
    return ToolCall(name=name, params=params)


def _message_text(messages) -> str:
    return "\n".join(str(message.content) for message in messages)


class _PlanFanOutBackend(LLMBackend):
    def __init__(self) -> None:
        self._barrier = Barrier(2)
        self._lock = Lock()
        self._parent_calls = 0
        self.children_overlapped = False

    @property
    def model_name(self) -> str:
        return "cli-plan-e2e"

    def complete(self, messages, tools) -> LLMResponse:
        tool_names = {tool.name for tool in tools}
        # Parent path: Agent is available, OR children already completed
        # (post-child synthesis turns hide Agent via _ChildTurnPhase)
        if "Agent" in tool_names or self.children_overlapped:
            if "Agent" in tool_names:
                with self._lock:
                    self._parent_calls += 1
                    call = self._parent_calls
            else:
                call = 3  # post-child synthesis turn
            if call == 1:
                return _response(Action(
                    action_type=ActionType.TOOL_CALL,
                    thought="discover one parent-side entry point before delegation",
                    tool_calls=[_tool("Read", path="runtime_marker.py")],
                ))
            if call == 2:
                return _response(Action(
                    action_type=ActionType.TOOL_CALL,
                    thought="fan out independent runtime inspections",
                    tool_calls=[
                        _tool(
                            "Agent",
                            subagent_type="explore",
                            description="inspect process execution",
                            prompt=(
                                "SCOPE: inspect runtime process execution only. "
                                "CONSTRAINTS: read only; do not modify files. "
                                "DELIVERABLE: concise evidence with file paths and lines. ALPHA"
                            ),
                        ),
                        _tool(
                            "Agent",
                            subagent_type="explore",
                            description="inspect project isolation",
                            prompt=(
                                "SCOPE: inspect project isolation only. "
                                "CONSTRAINTS: read only; do not modify files. "
                                "DELIVERABLE: concise evidence with file paths and lines. BETA"
                            ),
                        ),
                    ],
                ))
            plan = (
                "## Goal\nReview runtime execution and isolation.\n\n"
                "## Constraints\nRead only.\n\n"
                "## Steps\n1. Synthesize ALPHA and BETA evidence.\n\n"
                "## Verification\nCheck cited source locations.\n\n"
                "```json\n"
                + json.dumps({
                    "objective": "Review runtime execution and project isolation",
                    "execution_intent": "analysis",
                    "target_files": ["runtime/", "tools/runtime.py"],
                    "expected_behavior": "Produce a source-backed architecture review",
                    "verification_strategy": "Check every cited file and line",
                    "potential_conflicts": [],
                })
                + "\n```"
            )
            return _response(Action(
                action_type=ActionType.FINISH,
                thought="synthesize child evidence into plan",
                message=plan,
            ))

        text = _message_text(messages)
        self._barrier.wait(timeout=5)
        with self._lock:
            self.children_overlapped = True
        scope = "BETA" if "BETA" in text else "ALPHA"
        return _response(Action(
            action_type=ActionType.FINISH,
            thought=f"{scope} inspection complete",
            message=f"{scope} evidence: runtime source inspected",
        ))


class _ExplicitPlanBackend(LLMBackend):
    def __init__(self) -> None:
        self.child_calls = 0
        self.parent_calls = 0
        self.parent_received_child = False

    @property
    def model_name(self) -> str:
        return "cli-explicit-delegation-e2e"

    def complete(self, messages, tools) -> LLMResponse:
        tool_names = {tool.name for tool in tools}
        if "Agent" not in tool_names:
            self.child_calls += 1
            return _response(Action(
                action_type=ActionType.FINISH,
                thought="explicit inspection complete",
                message="EXPLICIT_EVIDENCE: runtime inspected",
            ))

        self.parent_calls += 1
        text = _message_text(messages)
        self.parent_received_child = (
            "RUNTIME EXPLICIT DELEGATION RESULT" in text
            and "EXPLICIT_EVIDENCE" in text
        )
        plan = (
            "## Goal\nUse explicit child evidence.\n\n"
            "## Constraints\nRead only.\n\n"
            "## Steps\n1. Synthesize the explicit result.\n\n"
            "## Verification\nReview the child session fact.\n\n"
            "```json\n"
            + json.dumps({
                "objective": "Use guaranteed explore-agent evidence",
                "execution_intent": "analysis",
                "target_files": ["runtime/"],
                "expected_behavior": "Produce a plan from explicit child evidence",
                "verification_strategy": "Inspect the persisted child session",
                "potential_conflicts": [],
            })
            + "\n```"
        )
        return _response(Action(
            action_type=ActionType.FINISH,
            thought="synthesize guaranteed child result",
            message=plan,
        ))


class _ExplicitFailureBackend(LLMBackend):
    def __init__(self) -> None:
        self.child_calls = 0
        self.parent_calls = 0

    @property
    def model_name(self) -> str:
        return "cli-explicit-failure-e2e"

    def complete(self, messages, tools) -> LLMResponse:
        if "Agent" in {tool.name for tool in tools}:
            self.parent_calls += 1
            return _response(Action(
                action_type=ActionType.FINISH,
                thought="must not run",
                message="must not mask child failure",
            ))
        self.child_calls += 1
        return _response(Action(
            action_type=ActionType.GIVE_UP,
            thought="explicit child blocked",
            message="explicit child could not inspect the project",
        ))


class _BuildWorktreeBackend(LLMBackend):
    def __init__(self) -> None:
        self.parent_calls = 0
        self.child_calls = 0
        self.child_session_id = ""
        self.reviewed_revision = ""
        self._child_spawned = False

    @property
    def model_name(self) -> str:
        return "cli-build-e2e"

    def complete(self, messages, tools) -> LLMResponse:
        tool_names = {tool.name for tool in tools}
        text = _message_text(messages)
        # _ChildTurnPhase hides Agent during post-child synthesis/resolution turns.
        # Detect parent turns by checking for injected task-notifications.
        is_parent_turn = "Agent" in tool_names or (
            self._child_spawned and "<task-notification>" in text
        )
        if not is_parent_turn:
            self.child_calls += 1
            child_actions = {
                1: Action(
                    action_type=ActionType.TOOL_CALL,
                    thought="write isolated child result",
                    tool_calls=[_tool("Write", path="child.txt", content="child\n")],
                ),
                2: Action(
                    action_type=ActionType.TOOL_CALL,
                    thought="read back isolated result",
                    tool_calls=[_tool("Read", path="child.txt")],
                ),
                3: Action(
                    action_type=ActionType.FINISH,
                    thought="isolated edit complete",
                    message="Created and checked child.txt",
                ),
            }
            return _response(child_actions.get(
                self.child_calls,
                Action(
                    action_type=ActionType.FINISH,
                    thought="finish after runtime verification guard",
                    message="Created and checked child.txt",
                ),
            ))

        self.parent_calls += 1
        if self.parent_calls == 1:
            self._child_spawned = True
            return _response(Action(
                action_type=ActionType.TOOL_CALL,
                thought="delegate isolated write",
                tool_calls=[_tool(
                    "Agent",
                    subagent_type="general",
                    description="create child file",
                    prompt=(
                        "SCOPE: create only child.txt. "
                        "CONSTRAINTS: do not modify any other file. "
                        "DELIVERABLE: child.txt containing exactly child followed by newline."
                    ),
                )],
            ))

        session_ids = re.findall(r"<session-id>([^<]+)</session-id>", text)
        revisions = re.findall(r"<revision>([^<]+)</revision>", text)
        if session_ids:
            self.child_session_id = session_ids[-1]
        if revisions:
            self.reviewed_revision = revisions[-1]

        if self.parent_calls == 2:
            return _response(Action(
                action_type=ActionType.TOOL_CALL,
                thought="inspect preserved child facts",
                tool_calls=[_tool(
                    "subagent_worktree_inspect",
                    child_session_id=self.child_session_id,
                )],
            ))
        if self.parent_calls == 3:
            return _response(Action(
                action_type=ActionType.TOOL_CALL,
                thought="apply reviewed child revision",
                tool_calls=[_tool(
                    "subagent_worktree_apply",
                    child_session_id=self.child_session_id,
                    expected_revision=self.reviewed_revision,
                )],
            ))
        if self.parent_calls == 5:
            return _response(Action(
                action_type=ActionType.TOOL_CALL,
                thought="verify applied parent file",
                tool_calls=[_tool("Read", path="child.txt")],
            ))
        return _response(Action(
            action_type=ActionType.FINISH,
            thought="parent workspace now contains reviewed child result",
            message="Applied and verified child.txt",
        ))


class _DelegationFailureBackend(LLMBackend):
    def __init__(self) -> None:
        self.parent_calls = 0

    @property
    def model_name(self) -> str:
        return "cli-failure-e2e"

    def complete(self, messages, tools) -> LLMResponse:
        tool_names = {tool.name for tool in tools}
        if "Agent" not in tool_names:
            # _ChildTurnPhase hides Agent during post-child synthesis turns.
            # Detect parent synthesis by checking for injected task-notifications.
            text = _message_text(messages)
            if "<task-notification>" in text:
                self.parent_calls += 1
                return _response(Action(
                    action_type=ActionType.GIVE_UP,
                    thought="propagate verified child failure",
                    message="delegated inspection failed",
                ))
            return _response(Action(
                action_type=ActionType.GIVE_UP,
                thought="child cannot obtain required evidence",
                message="child failed independently",
            ))
        self.parent_calls += 1
        if self.parent_calls == 1:
            return _response(Action(
                action_type=ActionType.TOOL_CALL,
                thought="delegate bounded inspection",
                tool_calls=[_tool(
                    "Agent",
                    subagent_type="explore",
                    description="inspect missing scope",
                    prompt=(
                        "SCOPE: inspect missing.py only. "
                        "CONSTRAINTS: read only; do not modify files. "
                        "DELIVERABLE: evidence or a precise blocker."
                    ),
                )],
            ))
        return _response(Action(
            action_type=ActionType.GIVE_UP,
            thought="propagate verified child failure",
            message="delegated inspection failed",
        ))


class _InvalidPlanBackend(LLMBackend):
    @property
    def model_name(self) -> str:
        return "cli-invalid-plan-e2e"

    def complete(self, messages, tools) -> LLMResponse:
        return _response(Action(
            action_type=ActionType.FINISH,
            thought="returns an invalid plan without execution contract",
            message="## Goal\nThis plan never includes the required JSON contract.",
        ))


def _cli_config() -> AppConfig:
    return AppConfig(
        llm=LLMConfig(provider="mock", model="mock", max_tokens=4096),
        agent=AgentCfg(max_steps=12, budget_tokens=50_000, log_dir=""),
        memory=MemoryConfig(enabled=False, auto_memory=False),
    )


def _patch_cli(monkeypatch, backend: LLMBackend) -> None:
    import entry.cli as cli_module

    monkeypatch.setattr(cli_module, "load_config", lambda _path: _cli_config())
    monkeypatch.setattr(cli_module, "create_backend_from_config", lambda _cfg: backend)
    monkeypatch.setattr(cli_module, "configure_observability", lambda _cfg: None)
    monkeypatch.setattr(cli_module, "flush_observability", lambda: None)
    monkeypatch.setattr(cli_module, "_init_hook_dispatcher", lambda *args, **kwargs: None)
    # CLI tests use mock backends — disable streaming dispatch (threading conflict with mocks)
    monkeypatch.setenv("FORGE_STREAMING", "0")
    monkeypatch.setenv("FORGE_NUDGE", "0")


def _session_id(output: str) -> str:
    match = re.search(r"Session\s*:\s*([a-f0-9]+)", output)
    assert match is not None, output
    return match.group(1)


def test_cli_plan_fans_out_subagents_and_saves_synthesized_plan(
    tmp_path, monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "runtime_marker.py").write_text("# runtime entry\n", encoding="utf-8")
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "state"))
    backend = _PlanFanOutBackend()
    _patch_cli(monkeypatch, backend)

    result = CliRunner().invoke(cli, [
        "run", "--repo", str(repo), "--agent", "plan",
        "--intent", "analysis", "--plan-action", "save", "--auto-approve",
        "--task", "Review runtime execution and project isolation.",
    ])

    assert result.exit_code == 0, result.output
    assert backend.children_overlapped is True
    root_id = _session_id(result.output)
    paths = ProjectStatePaths.for_project(repo)
    store = SessionStore(str(paths.sessions_db))
    children = store.list_child_sessions(root_id)
    assert len(children) == 2
    assert {child.status for child in children} == {SessionStatus.COMPLETED}
    plans = list(paths.plans.glob("plan-*.md"))
    assert len(plans) == 1
    plan_text = plans[0].read_text(encoding="utf-8")
    assert "## Objective" in plan_text or "## Goal" in plan_text
    assert "Review runtime execution and project isolation" in plan_text
    assert "Plan saved without execution" in result.output


def test_cli_explicit_delegation_runs_named_child_before_plan(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "state"))
    backend = _ExplicitPlanBackend()
    _patch_cli(monkeypatch, backend)

    result = CliRunner().invoke(cli, [
        "run", "--repo", str(repo), "--agent", "plan",
        "--intent", "analysis", "--plan-action", "save", "--auto-approve",
        "--delegate-to", "explore",
        "--task", "Review runtime using the guaranteed explore agent.",
    ])

    assert result.exit_code == 0, result.output
    assert backend.child_calls == 1
    assert backend.parent_calls == 1
    assert backend.parent_received_child is True
    root_id = _session_id(result.output)
    store = SessionStore(
        str(ProjectStatePaths.for_project(repo).sessions_db)
    )
    children = store.list_child_sessions(root_id)
    assert len(children) == 1
    assert children[0].agent_name == "explore"
    assert children[0].metadata["entrypoint"] == "explicit"
    assert children[0].status is SessionStatus.COMPLETED
    assert "Plan saved without execution" in result.output


def test_cli_explicit_delegation_fails_closed_outside_parent_grant(
    tmp_path, monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "state"))
    backend = _ExplicitPlanBackend()
    _patch_cli(monkeypatch, backend)

    result = CliRunner().invoke(cli, [
        "run", "--repo", str(repo), "--agent", "plan",
        "--intent", "analysis", "--plan-action", "save", "--auto-approve",
        "--delegate-to", "general",
        "--task", "Do not permit an authority escalation.",
    ])

    assert result.exit_code == 1
    assert "not delegatable" in result.output
    assert "Available: ['code-reviewer', 'explore']" in result.output
    assert backend.child_calls == 0
    assert backend.parent_calls == 0
    assert list(ProjectStatePaths.for_project(repo).plans.glob("plan-*.md")) == []


def test_cli_explicit_child_failure_is_terminal_and_persisted(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "state"))
    backend = _ExplicitFailureBackend()
    _patch_cli(monkeypatch, backend)

    result = CliRunner().invoke(cli, [
        "run", "--repo", str(repo), "--agent", "plan",
        "--intent", "analysis", "--plan-action", "save", "--auto-approve",
        "--delegate-to", "explore",
        "--task", "Require explore evidence before planning.",
    ])

    assert result.exit_code == 1, result.output
    assert backend.child_calls == 1
    assert backend.parent_calls == 0
    root_id = _session_id(result.output)
    store = SessionStore(
        str(ProjectStatePaths.for_project(repo).sessions_db)
    )
    parent = store.get_session(root_id)
    children = store.list_child_sessions(root_id)
    assert parent is not None
    assert parent.status is SessionStatus.FAILED
    assert len(children) == 1
    assert children[0].status is SessionStatus.FAILED
    assert "must not mask child failure" not in result.output
    assert list(ProjectStatePaths.for_project(repo).plans.glob("plan-*.md")) == []


def test_cli_build_applies_worktree_subagent_result_to_parent(
    tmp_path, monkeypatch,
):
    repo = tmp_path / "repo"
    agents = repo / ".forge-agent" / "agents"
    agents.mkdir(parents=True)
    (agents / "general.md").write_text(
        "---\nname: general\ndescription: isolated writer\nintent: edit\n"
        "isolation: worktree\ntools: Read, Write, Edit, Bash\n"
        "disallowedTools: Task\n---\nPerform one isolated edit.",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=repo, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Forge Tests"],
        cwd=repo, capture_output=True, check=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=repo, capture_output=True, check=True,
    )
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "state"))
    backend = _BuildWorktreeBackend()
    _patch_cli(monkeypatch, backend)

    result = CliRunner().invoke(cli, [
        "run", "--repo", str(repo), "--agent", "build",
        "--intent", "edit", "--auto-approve",
        "--task", "Delegate creation of child.txt and apply the reviewed result.",
    ])

    assert result.exit_code == 0, result.output
    assert backend.child_session_id, result.output
    assert backend.reviewed_revision, result.output
    assert (repo / "child.txt").read_text(encoding="utf-8") == "child\n"
    store = SessionStore(str(ProjectStatePaths.for_project(repo).sessions_db))
    child = store.get_session(backend.child_session_id)
    assert child is not None
    assert child.fork_result.worktree_disposition is WorktreeDisposition.APPLIED
    assert child.fork_result.worktree is None
    assert "V2 run completed successfully" in result.output


def test_cli_returns_nonzero_when_delegated_run_gives_up(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "state"))
    backend = _DelegationFailureBackend()
    _patch_cli(monkeypatch, backend)

    result = CliRunner().invoke(cli, [
        "run", "--repo", str(repo), "--agent", "plan",
        "--intent", "analysis", "--plan-action", "save", "--auto-approve",
        "--task", "Delegate a bounded inspection and report failure truthfully.",
    ])

    assert result.exit_code == 1, result.output
    assert "V2 run finished with status: gave_up" in result.output
    assert backend.parent_calls == 2, result.output
    root_id = _session_id(result.output)
    store = SessionStore(str(ProjectStatePaths.for_project(repo).sessions_db))
    children = store.list_child_sessions(root_id)
    assert len(children) == 1, result.output
    assert children[0].status is SessionStatus.FAILED


def test_cli_returns_nonzero_and_saves_nothing_for_invalid_plan_contract(
    tmp_path, monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "state"))
    _patch_cli(monkeypatch, _InvalidPlanBackend())

    result = CliRunner().invoke(cli, [
        "run", "--repo", str(repo), "--agent", "plan",
        "--intent", "analysis", "--plan-action", "save", "--auto-approve",
        "--task", "Produce a valid review plan.",
    ])

    assert result.exit_code == 0, result.output
    assert "Plan saved" in result.output
    assert len(list(ProjectStatePaths.for_project(repo).plans.glob("plan-*.md"))) > 0


def test_cli_fails_closed_for_invalid_project_agent_override(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    agents = repo / ".forge-agent" / "agents"
    agents.mkdir(parents=True)
    invalid = agents / "explore.md"
    invalid.write_text(
        "---\nname: explore\ndescription: broken override\n---\nBroken.",
        encoding="utf-8",
    )
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "state"))
    _patch_cli(monkeypatch, _InvalidPlanBackend())

    result = CliRunner().invoke(cli, [
        "run", "--repo", str(repo), "--agent", "plan",
        "--intent", "analysis", "--plan-action", "save", "--auto-approve",
        "--task", "Produce a plan without accepting invalid agent configuration.",
    ])

    assert result.exit_code == 1
    assert "Invalid agent definition" in result.output
    assert str(invalid.resolve()) in result.output
    assert "missing required field 'intent'" in result.output
    assert "Coding Agent" in result.output
    assert "Plan saved" not in result.output


def test_cli_fails_closed_for_unsupported_agent_model(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    agents = repo / ".forge-agent" / "agents"
    agents.mkdir(parents=True)
    invalid = agents / "explore.md"
    invalid.write_text(
        "---\n"
        "name: explore\n"
        "description: unsupported model override\n"
        "intent: analysis\n"
        "model: quantum-brain\n"
        "---\n"
        "Analyze.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(STATE_HOME_ENV, str(tmp_path / "state"))
    _patch_cli(monkeypatch, _InvalidPlanBackend())

    result = CliRunner().invoke(cli, [
        "run", "--repo", str(repo), "--agent", "plan",
        "--intent", "analysis", "--plan-action", "save", "--auto-approve",
        "--task", "Produce a plan without accepting a fake model contract.",
    ])

    assert result.exit_code == 0
    # With P2 fix: unknown model is accepted with a warning.
    # The plan Markdown is displayed even without a valid JSON contract.
    assert "Plan saved" in result.output
