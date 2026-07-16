from __future__ import annotations

import json
import tempfile
from pathlib import Path

from observability.validation import (
    build_baseline_snapshot,
    default_baseline_output_path,
    ValidationResult,
    evaluate_validation_result,
    failure_dataset_path_for,
    failure_dataset_line_count,
    get_langfuse_validation_scenarios,
    selected_validation_scenarios,
    write_baseline_snapshot,
    write_validation_results,
)


def test_validation_scenarios_cover_success_and_failure() -> None:
    scenarios = get_langfuse_validation_scenarios()

    assert [scenario.name for scenario in scenarios] == [
        "success-readonly",
        "failure-low-budget",
    ]
    assert scenarios[0].expected_status == "success"
    assert scenarios[1].expected_status == "gave_up"
    assert scenarios[1].expect_failure_dataset_increment is True


def test_evaluate_validation_result_checks_dataset_behavior() -> None:
    success_scenario, failure_scenario = get_langfuse_validation_scenarios()

    passed_success, success_checks = evaluate_validation_result(
        success_scenario,
        actual_status="success",
        trace_id="trace-1",
        dataset_new_entries=0,
    )
    passed_failure, failure_checks = evaluate_validation_result(
        failure_scenario,
        actual_status="gave_up",
        trace_id="trace-2",
        dataset_new_entries=1,
    )

    assert passed_success is True
    assert success_checks["dataset_behavior_ok"] is True
    assert passed_failure is True
    assert failure_checks["dataset_behavior_ok"] is True


def test_failure_dataset_line_count_and_json_report() -> None:
    with tempfile.TemporaryDirectory(dir=".") as tmp_dir:
        repo_path = Path(tmp_dir)
        dataset_path = failure_dataset_path_for(str(repo_path))
        dataset_path.parent.mkdir(parents=True, exist_ok=True)
        dataset_path.write_text('{"id":"x"}\n{"id":"y"}\n', encoding="utf-8")

        resolved_dataset_path, line_count = failure_dataset_line_count(str(repo_path))
        assert resolved_dataset_path == dataset_path
        assert line_count == 2

        report_path = repo_path / "validation-report.json"
        written = write_validation_results(
            [
                ValidationResult(
                    scenario="success-readonly",
                    expected_status="success",
                    actual_status="success",
                    passed=True,
                    repo_path=str(repo_path),
                    summary="ok",
                    steps=2,
                    tokens=123,
                    log_path="logs/demo.jsonl",
                    trace_id="trace-1",
                    trace_url="https://example.com/trace-1",
                )
            ],
            report_path,
        )

        payload = json.loads(written.read_text(encoding="utf-8"))
        assert payload["all_passed"] is True
        assert payload["results"][0]["scenario"] == "success-readonly"


def test_selected_validation_scenarios_support_both_and_single() -> None:
    both = selected_validation_scenarios("both")
    single = selected_validation_scenarios("success-readonly")

    assert len(both) == 2
    assert len(single) == 1
    assert single[0].name == "success-readonly"


def test_build_and_write_baseline_snapshot() -> None:
    with tempfile.TemporaryDirectory(dir=".") as tmp_dir:
        repo_path = Path(tmp_dir)
        result = ValidationResult(
            scenario="success-readonly",
            expected_status="success",
            actual_status="success",
            passed=True,
            repo_path=str(repo_path),
            summary="ok",
            steps=2,
            tokens=200,
            log_path="logs/demo.jsonl",
            trace_id="trace-1",
            trace_url="https://example.com/trace-1",
        )
        snapshot = build_baseline_snapshot(
            baseline_name="nightly-main",
            repo_path=str(repo_path),
            provider="deepseek",
            model="deepseek-v4-flash",
            prompt_source="local",
            prompt_label="production",
            prompt_version=None,
            results=[result],
            metadata={"source": "test"},
        )

        assert snapshot.all_passed is True
        assert snapshot.pass_rate == 1.0
        assert snapshot.average_tokens == 200.0
        assert snapshot.scenarios == ["success-readonly"]

        default_path = default_baseline_output_path(str(repo_path), "nightly-main")
        assert default_path.name == "nightly-main.json"

        written = write_baseline_snapshot(snapshot, default_path)
        payload = json.loads(written.read_text(encoding="utf-8"))
        assert payload["baseline_name"] == "nightly-main"
        assert payload["provider"] == "deepseek"
        assert payload["results"][0]["trace_id"] == "trace-1"
