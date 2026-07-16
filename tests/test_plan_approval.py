from entry.modes.interaction import (
    ApprovalAction,
    ApprovalChoice,
    ClickAdapter,
    PlanExecutionPolicy,
    PredefinedChoiceAdapter,
    cli_plan_adapter,
)
from entry.modes.plan_approval import PlanAction, PlanApprovalService
from agent.task import TaskIntent


def _valid_contract_data():
    return {
        "objective": "Review runtime process execution and project isolation",
        "execution_intent": "analysis",
        "target_files": ["runtime/project_environment.py"],
        "expected_behavior": "Document the implementation with exact source locations",
        "verification_strategy": "Read the cited source lines",
        "potential_conflicts": [],
    }


def test_v2_runner_declares_typed_plan_intent():
    from entry.modes import v2_runner

    assert v2_runner.TaskIntent is TaskIntent


def test_plan_contract_extraction_skips_markdown_brackets_before_json():
    import json

    from entry.modes.plan_contract import extract_and_parse_json

    data = _valid_contract_data()
    text = "# Report\n[ENVIRONMENT] cwd={not-json}\n```json\n" + json.dumps(data) + "\n```"

    assert extract_and_parse_json(text) == data


def test_canonical_plan_document_is_human_readable_and_machine_parseable():
    from entry.modes.plan_contract import PlanContract, extract_and_parse_json

    contract = PlanContract.model_validate(_valid_contract_data())
    document = contract.render_plan_document()

    assert document.startswith("## Objective")
    assert "## Execution Contract\n```json" in document
    assert extract_and_parse_json(document) == contract.model_dump()


def test_plan_filename_is_stable_and_does_not_embed_task_text():
    from entry.modes.v2_runner import _plan_filename

    description = "审查 runtime 目录，并列出文件和行号。"

    assert _plan_filename(description) == _plan_filename(description)
    assert _plan_filename(description).startswith("plan-")
    assert description[:2] not in _plan_filename(description)


def test_plan_contract_preserves_declared_step_and_token_limits():
    from types import SimpleNamespace

    from agent.v2.task_contract import TaskContract

    contract = TaskContract.for_plan(SimpleNamespace(
        max_steps=40, budget_tokens=80_000,
    ))

    assert contract.max_steps == 40
    assert contract.budget_tokens == 80_000


def test_approval_choice_coerces_external_string_at_boundary():
    choice = ApprovalChoice(action="revise", feedback="Add tests")

    assert choice.action is ApprovalAction.REVISE


def test_execution_choices_trigger_build():
    service = PlanApprovalService()

    assert service.evaluate(
        ApprovalChoice(ApprovalAction.EXECUTE)
    ) is PlanAction.TRIGGER_BUILD


def test_save_choice_completes_plan_without_build():
    service = PlanApprovalService()

    assert service.evaluate(
        ApprovalChoice(ApprovalAction.SAVE)
    ) is PlanAction.COMPLETE_PLAN


def test_revision_limit_is_committed_only_after_execution():
    service = PlanApprovalService(max_revisions=1)
    choice = ApprovalChoice(ApprovalAction.REVISE, feedback="Add tests")

    assert service.evaluate(choice) is PlanAction.TRIGGER_REPLAN
    assert service.revision_count == 0
    service.commit_revision()
    assert service.evaluate(choice) is PlanAction.ABORT_REVISIONS


def test_adapters_return_typed_actions():
    assert PredefinedChoiceAdapter("abort").prompt_approval().action is ApprovalAction.ABORT
    assert cli_plan_adapter(PlanExecutionPolicy.SAVE).prompt_approval().action is ApprovalAction.SAVE
    assert cli_plan_adapter(PlanExecutionPolicy.EXECUTE).prompt_approval().action is ApprovalAction.EXECUTE


def test_click_adapter_displays_plan_and_defaults_to_save(monkeypatch, capsys):
    monkeypatch.setattr("click.prompt", lambda *args, **kwargs: kwargs["default"])
    adapter = ClickAdapter()

    adapter.show_plan("## Objective\nInspect runtime", "plan-123.md")
    choice = adapter.prompt_approval()

    output = capsys.readouterr().out
    assert "## Objective\nInspect runtime" in output
    assert "Save plan and exit (default)" in output
    assert choice.action is ApprovalAction.SAVE


def test_cli_separates_tool_auto_approval_from_plan_execution():
    from click.testing import CliRunner
    from entry.cli import cli

    result = CliRunner().invoke(cli, ["run", "--help"])

    assert result.exit_code == 0
    assert "--plan-action [review|save|execute]" in result.output
    assert "does not execute a generated plan" in " ".join(result.output.split())


def test_manual_plan_edit_reads_known_path_without_editor_lookup(tmp_path, monkeypatch):
    from entry.modes import v2_runner

    plan_path = tmp_path / "plan.md"
    plan_path.write_text("updated plan", encoding="utf-8")
    messages = []
    interaction = type(
        "Interaction",
        (),
        {"show_message": lambda self, text, style: messages.append((text, style))},
    )()
    paused = []
    monkeypatch.setattr(v2_runner.click, "pause", lambda prompt: paused.append(prompt))

    result = v2_runner._read_manual_plan_edit(str(plan_path.resolve()), interaction)

    assert result == "updated plan"
    assert str(plan_path.resolve()) in messages[0][0]
    assert paused


def test_v2_result_printer_suppresses_summary_already_rendered_by_events(capsys):
    from types import SimpleNamespace

    from agent.task import RunStatus
    from entry.modes.v2_runner import _print_v2_result

    result = SimpleNamespace(status=RunStatus.SUCCESS, summary="unique final report")

    _print_v2_result("v2-build", "sessions.db", "session-id", result, show_summary=False)

    output = capsys.readouterr().out
    assert "unique final report" not in output
    assert "completed successfully" in output


def test_v2_plan_e2e_saves_canonical_plan_without_executing(
    tmp_path, monkeypatch,
):
    import json
    import sqlite3

    from agent.core import AgentConfig
    from agent.task import Action, ActionType
    from entry.modes.interaction import PredefinedChoiceAdapter
    from entry.modes.plan_contract import extract_and_parse_json
    from entry.modes.v2_runner import _plan_filename, run_v2_mode
    from llm.base import MockBackend
    from runtime.state_paths import ProjectStatePaths, STATE_HOME_ENV
    from tools.base import ToolRegistry

    repo = tmp_path / "target-repo"
    repo.mkdir()
    marker = repo / "runtime.py"
    marker.write_text("# process runtime\n", encoding="utf-8")
    state_home = tmp_path / "isolated-state"
    monkeypatch.setenv(STATE_HOME_ENV, str(state_home))
    description = "Review runtime process execution and project isolation"
    contract = _valid_contract_data()
    model_output = (
        "## Implementation plan\nInspect the runtime boundary.\n```json\n"
        + json.dumps(contract)
        + "\n```"
    )
    backend = MockBackend([
        Action(
            action_type=ActionType.FINISH,
            thought="plan complete",
            message=model_output,
        ),
    ], input_tokens=100, output_tokens=100)

    run_v2_mode(
        agent_name="plan",
        description=description,
        repo_path=repo,
        backend=backend,
        registry=ToolRegistry(),
        agent_config=AgentConfig(
            max_steps=10,
            budget_tokens=5_000,
            request_budget_tokens=4_000,
            stream=False,
        ),
        memory_context=None,
        log_dir="",
        intent_override="analysis",
        approval_interaction=PredefinedChoiceAdapter("save"),
        renderer=None,
    )

    paths = ProjectStatePaths.for_project(repo)
    plan_path = paths.plans / _plan_filename(description)
    assert plan_path.is_file()
    plan_text = plan_path.read_text(encoding="utf-8")
    assert plan_text.startswith("## Objective")
    assert extract_and_parse_json(plan_text) == contract
    assert marker.read_text(encoding="utf-8") == "# process runtime\n"
    assert backend.call_count == 1

    with sqlite3.connect(paths.sessions_db) as connection:
        sessions = connection.execute(
            "SELECT agent_name, status FROM sessions ORDER BY created_at"
        ).fetchall()
    assert sessions == [("plan", "completed")]
