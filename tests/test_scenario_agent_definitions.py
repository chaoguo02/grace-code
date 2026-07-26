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
)
from agent.task import TaskIntent


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_WORKERS = {
    "plan-researcher",
    "debugger",
    "test-runner",
    "security-reviewer",
}


def test_project_catalog_exposes_research_primary_and_leaf_workers(
    tmp_path: Path,
) -> None:
    definitions = load_agent_definitions(
        project_dir=PROJECT_ROOT,
        user_dir=tmp_path / "empty-user-agents",
    )

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

    assert {"research", "explore", *SCENARIO_WORKERS} <= definitions.keys()
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
