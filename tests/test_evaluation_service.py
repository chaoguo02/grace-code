from __future__ import annotations

import json
from pathlib import Path

from server.services.evaluation_service import EvaluationService


def _result(
    scenario: str,
    *,
    passed: bool,
    tokens: int,
    actual_status: str = "success",
) -> dict:
    return {
        "scenario": scenario,
        "expected_status": "success",
        "actual_status": actual_status,
        "passed": passed,
        "repo_path": ".",
        "summary": "done",
        "steps": 3,
        "tokens": tokens,
        "log_path": "run.jsonl",
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_evaluation_overview_reads_runs_and_computes_regression(
    tmp_path: Path,
) -> None:
    baseline = {
        "baseline_name": "stable",
        "created_at": "2026-07-25T00:00:00+00:00",
        "provider": "openai",
        "model": "baseline-model",
        "prompt_source": "local",
        "prompt_label": "production",
        "prompt_version": 3,
        "pass_rate": 1.0,
        "average_tokens": 1000,
        "results": [_result("success-readonly", passed=True, tokens=1000)],
    }
    _write_json(
        tmp_path / "ci" / "langfuse-baselines" / "stable.json",
        baseline,
    )
    report = {
        "all_passed": True,
        "results": [_result("success-readonly", passed=True, tokens=1400)],
    }
    _write_json(
        tmp_path
        / ".grace"
        / "ci"
        / "langfuse"
        / "20260726T010203Z"
        / "validation-report.json",
        report,
    )

    overview = EvaluationService(str(tmp_path)).get_overview()

    assert len(overview["runs"]) == 1
    assert len(overview["baselines"]) == 1
    assert "payload" not in overview["baselines"][0]
    run = overview["runs"][0]
    assert run["pass_rate"] == 1.0
    assert run["comparison_source"] == "computed"
    assert run["comparison"]["passed"] is False
    assert overview["summary"]["regression_count"] == 2


def test_evaluation_overview_keeps_catalog_without_artifacts(
    tmp_path: Path,
) -> None:
    overview = EvaluationService(str(tmp_path)).get_overview()

    assert overview["runs"] == []
    assert overview["baselines"] == []
    assert overview["summary"]["run_count"] == 0
    assert {item["name"] for item in overview["scenario_catalog"]} == {
        "success-readonly",
        "failure-low-budget",
    }


def test_evaluation_overview_ignores_malformed_artifacts(
    tmp_path: Path,
) -> None:
    malformed = (
        tmp_path
        / ".forge-agent"
        / "ci"
        / "langfuse"
        / "broken"
        / "validation-report.json"
    )
    malformed.parent.mkdir(parents=True)
    malformed.write_text("{not-json", encoding="utf-8")

    overview = EvaluationService(str(tmp_path)).get_overview()

    assert overview["runs"] == []
