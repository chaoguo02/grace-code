from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.schema import load_config
@dataclass(frozen=True)
class ValidationScenario:
    name: str
    description: str
    expected_status: str
    max_steps: int
    budget_tokens: int
    intent: str = "analysis"
    mode: str = "react"
    expect_failure_dataset_increment: bool = False


@dataclass
class ValidationResult:
    scenario: str
    expected_status: str
    actual_status: str
    passed: bool
    repo_path: str
    summary: str
    steps: int
    tokens: int
    log_path: str
    trace_id: str | None = None
    trace_url: str | None = None
    dataset_path: str | None = None
    dataset_lines_before: int = 0
    dataset_lines_after: int = 0
    dataset_new_entries: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BaselineSnapshot:
    baseline_name: str
    created_at: str
    repo_path: str
    provider: str
    model: str
    prompt_source: str
    prompt_label: str
    prompt_version: int | None
    scenarios: list[str]
    all_passed: bool
    pass_rate: float
    average_tokens: float
    results: list[ValidationResult] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["results"] = [result.to_dict() for result in self.results]
        return payload


def get_langfuse_validation_scenarios() -> list[ValidationScenario]:
    task_text = (
        "Read pyproject.toml and answer: what is the project name and does it depend on "
        "langfuse? Use repository files only."
    )
    return [
        ValidationScenario(
            name="success-readonly",
            description=task_text,
            expected_status="success",
            max_steps=4,
            budget_tokens=30_000,
        ),
        ValidationScenario(
            name="failure-low-budget",
            description=task_text,
            expected_status="gave_up",
            max_steps=4,
            budget_tokens=4_000,
            expect_failure_dataset_increment=True,
        ),
    ]


def get_validation_scenario(name: str) -> ValidationScenario:
    for scenario in get_langfuse_validation_scenarios():
        if scenario.name == name:
            return scenario
    raise ValueError(f"Unknown validation scenario: {name!r}")


def selected_validation_scenarios(selection: str) -> list[ValidationScenario]:
    if selection == "both":
        return get_langfuse_validation_scenarios()
    return [get_validation_scenario(selection)]


def failure_dataset_path_for(repo_path: str) -> Path:
    from runtime.state_paths import ProjectStatePaths
    return ProjectStatePaths.for_project(repo_path).datasets


def failure_dataset_line_count(repo_path: str) -> tuple[Path, int]:
    dataset_path = failure_dataset_path_for(repo_path)
    if not dataset_path.exists():
        return dataset_path, 0
    return dataset_path, len(dataset_path.read_text(encoding="utf-8").splitlines())


def evaluate_validation_result(
    scenario: ValidationScenario,
    *,
    actual_status: str,
    trace_id: str | None,
    dataset_new_entries: int,
) -> tuple[bool, dict[str, Any]]:
    checks = {
        "status_matches": actual_status == scenario.expected_status,
        "trace_captured": bool(trace_id),
        "dataset_behavior_ok": (
            dataset_new_entries >= 1
            if scenario.expect_failure_dataset_increment
            else dataset_new_entries == 0
        ),
    }
    return all(checks.values()), checks


def write_validation_results(results: list[ValidationResult], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "results": [result.to_dict() for result in results],
        "all_passed": all(result.passed for result in results),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def default_baseline_output_path(repo_path: str, baseline_name: str) -> Path:
    from runtime.state_paths import ProjectStatePaths
    safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in baseline_name).strip("-_")
    safe_name = safe_name or "langfuse-baseline"
    return ProjectStatePaths.for_project(repo_path).experiments / "langfuse-baselines" / f"{safe_name}.json"


def build_baseline_snapshot(
    *,
    baseline_name: str,
    repo_path: str,
    provider: str,
    model: str,
    prompt_source: str,
    prompt_label: str,
    prompt_version: int | None,
    results: list[ValidationResult],
    metadata: dict[str, Any] | None = None,
) -> BaselineSnapshot:
    passed_count = sum(1 for result in results if result.passed)
    total = len(results)
    average_tokens = (sum(result.tokens for result in results) / total) if total else 0.0
    return BaselineSnapshot(
        baseline_name=baseline_name,
        created_at=datetime.now(timezone.utc).isoformat(),
        repo_path=repo_path,
        provider=provider,
        model=model,
        prompt_source=prompt_source,
        prompt_label=prompt_label,
        prompt_version=prompt_version,
        scenarios=[result.scenario for result in results],
        all_passed=all(result.passed for result in results),
        pass_rate=(passed_count / total) if total else 0.0,
        average_tokens=average_tokens,
        results=results,
        metadata=metadata or {},
    )


def write_baseline_snapshot(snapshot: BaselineSnapshot, output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_validation_config(config_path: str | Path | None):
    config = load_config(config_path)
    config.observability.enabled = True
    config.observability.provider = "langfuse"
    config.memory.enabled = False
    return config
