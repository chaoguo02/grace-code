from __future__ import annotations

from pathlib import Path

import pytest

from agent.session.agent_definition import (
    AgentDefinitionError,
    load_agent_definitions,
)
from agent.session.models import (
    AgentKind,
    AgentVisibility,
    DelegationMode,
    DelegationScope,
    WorkspaceMode,
)
from agent.task import TaskIntent


def test_temp_project_orchestrator_session_exposes_agent_batch_schema(
    tmp_path: Path,
) -> None:
    """A fixture repo must expose the session-effective batch tool to the LLM."""
    from agent.session.agent_registry import AgentRegistryV2
    from agent.session.models import SessionMode
    from agent.session.registry_builder import build_registry_for_session
    from agent.session.session_store import SessionStore
    from core.base import ToolRegistry

    project = tmp_path / "fixture"
    project.mkdir()
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(
        agent_name="orchestrator",
        mode=SessionMode.PRIMARY,
        repo_path=str(project),
        title="isolated multi-agent smoke",
    )
    agent_registry = AgentRegistryV2(project_dir=project)
    spec = agent_registry.get("orchestrator")

    effective_registry = build_registry_for_session(
        spec,
        session,
        base_registry=ToolRegistry(),
        agent_registry=agent_registry,
        runtime=__import__("types").SimpleNamespace(
            agent_registry=agent_registry,
        ),
    )

    assert session.agent_name == "orchestrator"
    assert "AgentBatch" in effective_registry.tool_names
    assert "AgentBatch" in {
        schema.name for schema in effective_registry.get_schemas()
    }


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_WORKERS = {
    "plan-researcher",
    "debugger",
    "test-runner",
    "security-reviewer",
}
ORCHESTRATOR_SUBAGENTS = frozenset({
    "explore",
    "general",
    "debugger",
    "test-runner",
    "code-reviewer",
    "security-reviewer",
})
ORCHESTRATOR_COORDINATION_TOOLS = frozenset({
    "Agent",
    "AgentBatch",
    "subagent_worktree_inspect",
    "subagent_worktree_apply",
    "subagent_worktree_discard",
    "subagent_worktree_retain",
})


def _assert_orchestrator_contract(definitions: dict) -> None:
    orchestrator = definitions["orchestrator"]
    assert orchestrator.agent_kind is AgentKind.PRIMARY
    assert orchestrator.intent is TaskIntent.EDIT
    assert orchestrator.permission_mode == "default"
    assert orchestrator.delegation_policy.mode is DelegationMode.ALLOWLIST
    assert orchestrator.delegation_policy.allowed_names == ORCHESTRATOR_SUBAGENTS
    assert {
        "Read", "Glob", "Grep", "Write", "Edit", "Bash",
        "git_status", "git_diff", "pytest",
    } <= orchestrator.tools
    assert ORCHESTRATOR_COORDINATION_TOOLS <= orchestrator.tools
    assert {"ProposeAgentTeam", "TeamCoordinate"}.isdisjoint(orchestrator.tools)

    general = definitions["general"]
    assert general.workspace_mode is WorkspaceMode.WORKTREE
    assert orchestrator.permits_subagent(definitions["explore"])
    assert orchestrator.permits_subagent(general)
    assert orchestrator.permits_subagent(definitions["code-reviewer"])


def test_project_catalog_exposes_research_primary_and_leaf_workers(
    tmp_path: Path,
) -> None:
    definitions = load_agent_definitions(
        project_dir=PROJECT_ROOT,
        user_dir=tmp_path / "empty-user-agents",
    )

    _assert_orchestrator_contract(definitions)

    research = definitions["research"]
    assert research.agent_kind is AgentKind.PRIMARY
    assert research.intent is TaskIntent.ANALYSIS
    assert research.delegation_scope is DelegationScope.READ_ONLY
    assert research.delegation_policy.allowed_names == frozenset(
        {"explore", "code-reviewer", "security-reviewer"}
    )
    assert "Agent" in research.tools
    assert "Bash" not in research.tools
    assert {"Write", "Edit", "Bash"} <= research.disallowed_tools

    explore = definitions["explore"]
    assert explore.agent_kind is AgentKind.NAMED_SUBAGENT
    assert explore.delegation_policy.mode is DelegationMode.DISABLED
    assert "Agent" in explore.disallowed_tools

    for name in SCENARIO_WORKERS:
        worker = definitions[name]
        assert worker.agent_kind is AgentKind.NAMED_SUBAGENT
        assert worker.intent is TaskIntent.ANALYSIS
        assert worker.delegation_policy.mode is DelegationMode.DISABLED
        assert "Agent" in worker.disallowed_tools


def test_specialized_worker_permissions_and_completion_contracts(
    tmp_path: Path,
) -> None:
    definitions = load_agent_definitions(
        project_dir=PROJECT_ROOT,
        user_dir=tmp_path / "empty-user-agents",
    )

    plan_researcher = definitions["plan-researcher"]
    assert {"Write", "Edit", "Bash", "Agent"} <= plan_researcher.disallowed_tools
    assert plan_researcher.permission_mode == "plan"

    debugger = definitions["debugger"]
    assert {"Bash", "pytest", "ReportFindings"} <= debugger.tools
    assert debugger.required_tools == frozenset({"ReportFindings"})
    assert debugger.completion_requires == {"ReportFindings": 1}

    test_runner = definitions["test-runner"]
    assert {"Bash", "pytest"} <= test_runner.tools
    assert {"Write", "Edit", "Agent"} <= test_runner.disallowed_tools

    security = definitions["security-reviewer"]
    assert security.visibility is AgentVisibility.HIDDEN
    assert "Bash" in security.disallowed_tools
    assert security.required_tools == frozenset({"ReportFindings"})
    assert security.completion_requires == {"ReportFindings": 1}


def test_builtin_catalog_contains_scenario_agents_without_project_files(
    tmp_path: Path,
) -> None:
    definitions = load_agent_definitions(
        project_dir=tmp_path / "empty-project",
        user_dir=tmp_path / "empty-user-agents",
    )

    assert {"orchestrator", "research", "explore", *SCENARIO_WORKERS} <= definitions.keys()
    _assert_orchestrator_contract(definitions)

    project_definitions = load_agent_definitions(
        project_dir=PROJECT_ROOT,
        user_dir=tmp_path / "empty-user-agents",
    )
    builtin = definitions["orchestrator"]
    project = project_definitions["orchestrator"]
    assert project.description == builtin.description
    assert project.intent is builtin.intent
    assert project.agent_kind is builtin.agent_kind
    assert project.tools == builtin.tools
    assert project.delegation_policy == builtin.delegation_policy
    assert project.permission_mode == builtin.permission_mode
    assert project.max_turns == builtin.max_turns
    assert project.system_prompt.strip() == builtin.system_prompt.strip()

    assert definitions["research"].agent_kind is AgentKind.PRIMARY
    assert definitions["explore"].agent_kind is AgentKind.NAMED_SUBAGENT


def test_project_definition_overrides_builtin_definition(tmp_path: Path) -> None:
    agents_dir = tmp_path / ".grace" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "research.md").write_text(
        """---
name: research
description: Project-specific research primary.
intent: analysis
kind: primary
tools: Read, Agent
allowedSubagents: explore
delegationScope: read_only
maxTurns: 17
---
Project override.
""",
        encoding="utf-8",
    )

    definitions = load_agent_definitions(
        project_dir=tmp_path,
        user_dir=tmp_path / "empty-user-agents",
    )

    assert definitions["research"].description == "Project-specific research primary."
    assert definitions["research"].max_turns == 17
    assert definitions["research"].system_prompt.strip() == "Project override."


def test_runtime_contract_frontmatter_accepts_camel_and_snake_case(
    tmp_path: Path,
) -> None:
    camel_dir = tmp_path / "camel"
    camel_dir.mkdir()
    (camel_dir / "camel.md").write_text(
        """---
name: camel
description: Camel case contract.
intent: analysis
tools: Read, ReportFindings
requiredTools: [Read, ReportFindings]
completionRequires:
  ReportFindings: 2
---
Report.
""",
        encoding="utf-8",
    )
    snake_dir = tmp_path / "snake"
    snake_dir.mkdir()
    (snake_dir / "snake.md").write_text(
        """---
name: snake
description: Snake case contract.
intent: analysis
tools: Read, ReportFindings
required_tools: ReportFindings
completion_requires:
  ReportFindings: 1
---
Report.
""",
        encoding="utf-8",
    )

    camel = load_agent_definitions(user_dir=camel_dir)["camel"]
    snake = load_agent_definitions(user_dir=snake_dir)["snake"]

    assert camel.required_tools == frozenset({"Read", "ReportFindings"})
    assert camel.completion_requires == {"ReportFindings": 2}
    assert snake.required_tools == frozenset({"ReportFindings"})
    assert snake.completion_requires == {"ReportFindings": 1}


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("requiredTools", "{bad: shape}", "requiredTools"),
        ("completionRequires", "[ReportFindings]", "completionRequires"),
        (
            "completionRequires",
            "{ReportFindings: 0}",
            "positive integers",
        ),
        (
            "completionRequires",
            "{ReportFindings: true}",
            "positive integers",
        ),
    ],
)
def test_runtime_contract_frontmatter_rejects_invalid_values(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    agents_dir = tmp_path / ".grace" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "invalid.md").write_text(
        f"""---
name: invalid
description: Invalid contract.
intent: analysis
tools: Read, ReportFindings
{field}: {value}
---
Report.
""",
        encoding="utf-8",
    )

    with pytest.raises(AgentDefinitionError, match=message):
        load_agent_definitions(
            project_dir=tmp_path,
            user_dir=tmp_path / "empty-user-agents",
        )
