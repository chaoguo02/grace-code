from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ComparisonCheck:
    name: str
    passed: bool
    details: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BaselineComparison:
    report_path: str | None
    baseline_path: str | None
    passed: bool
    checks: list[ComparisonCheck] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_path": self.report_path,
            "baseline_path": self.baseline_path,
            "passed": self.passed,
            "checks": [check.to_dict() for check in self.checks],
            "metadata": self.metadata,
        }


def load_json_file(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json_file(path: str | Path, payload: dict[str, Any]) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def write_text_file(path: str | Path, content: str) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return output_path


def compare_validation_report_to_baseline(
    report_payload: dict[str, Any],
    baseline_payload: dict[str, Any],
    *,
    max_token_regression_pct: float = 0.2,
    report_path: str | None = None,
    baseline_path: str | None = None,
) -> BaselineComparison:
    report_results = report_payload.get("results", [])
    baseline_results = baseline_payload.get("results", [])
    report_index = {item["scenario"]: item for item in report_results}
    baseline_index = {item["scenario"]: item for item in baseline_results}

    report_scenarios = set(report_index)
    baseline_scenarios = set(baseline_index)
    missing_scenarios = sorted(baseline_scenarios - report_scenarios)
    unexpected_scenarios = sorted(report_scenarios - baseline_scenarios)

    current_pass_rate = _compute_pass_rate(report_results)
    baseline_pass_rate = float(baseline_payload.get("pass_rate", _compute_pass_rate(baseline_results)))
    current_average_tokens = _compute_average_tokens(report_results)
    baseline_average_tokens = float(baseline_payload.get("average_tokens", _compute_average_tokens(baseline_results)))

    checks: list[ComparisonCheck] = [
        ComparisonCheck(
            name="report_all_passed",
            passed=bool(report_payload.get("all_passed")),
            details=f"report all_passed={report_payload.get('all_passed')}",
        ),
        ComparisonCheck(
            name="scenario_set_matches",
            passed=not missing_scenarios and not unexpected_scenarios,
            details=(
                f"missing={missing_scenarios or '[]'}, "
                f"unexpected={unexpected_scenarios or '[]'}"
            ),
        ),
        ComparisonCheck(
            name="pass_rate_not_worse",
            passed=current_pass_rate >= baseline_pass_rate,
            details=f"current={current_pass_rate:.3f}, baseline={baseline_pass_rate:.3f}",
        ),
    ]

    if baseline_average_tokens > 0:
        allowed_average_tokens = baseline_average_tokens * (1 + max_token_regression_pct)
        checks.append(
            ComparisonCheck(
                name="average_tokens_within_threshold",
                passed=current_average_tokens <= allowed_average_tokens,
                details=(
                    f"current={current_average_tokens:.1f}, baseline={baseline_average_tokens:.1f}, "
                    f"allowed<={allowed_average_tokens:.1f}"
                ),
            )
        )
    else:
        checks.append(
            ComparisonCheck(
                name="average_tokens_within_threshold",
                passed=True,
                details="baseline average_tokens is 0; skipping threshold check",
            )
        )

    for scenario in sorted(baseline_scenarios & report_scenarios):
        current = report_index[scenario]
        baseline = baseline_index[scenario]
        checks.append(
            ComparisonCheck(
                name=f"scenario:{scenario}:status_matches",
                passed=current.get("actual_status") == baseline.get("actual_status"),
                details=(
                    f"current={current.get('actual_status')}, "
                    f"baseline={baseline.get('actual_status')}"
                ),
            )
        )

        baseline_tokens = int(baseline.get("tokens", 0) or 0)
        current_tokens = int(current.get("tokens", 0) or 0)
        if baseline_tokens > 0:
            allowed_tokens = baseline_tokens * (1 + max_token_regression_pct)
            checks.append(
                ComparisonCheck(
                    name=f"scenario:{scenario}:tokens_within_threshold",
                    passed=current_tokens <= allowed_tokens,
                    details=(
                        f"current={current_tokens}, baseline={baseline_tokens}, "
                        f"allowed<={allowed_tokens:.1f}"
                    ),
                )
            )
        else:
            checks.append(
                ComparisonCheck(
                    name=f"scenario:{scenario}:tokens_within_threshold",
                    passed=True,
                    details="baseline tokens is 0; skipping threshold check",
                )
            )

    passed = all(check.passed for check in checks)
    return BaselineComparison(
        report_path=report_path,
        baseline_path=baseline_path,
        passed=passed,
        checks=checks,
        metadata={
            "current_pass_rate": current_pass_rate,
            "baseline_pass_rate": baseline_pass_rate,
            "current_average_tokens": current_average_tokens,
            "baseline_average_tokens": baseline_average_tokens,
            "max_token_regression_pct": max_token_regression_pct,
            "missing_scenarios": missing_scenarios,
            "unexpected_scenarios": unexpected_scenarios,
        },
    )


def render_ci_markdown_summary(
    report_payload: dict[str, Any] | None,
    *,
    comparison: BaselineComparison | None = None,
    cli_exit_code: int = 0,
    compare_baseline_path: str | None = None,
) -> str:
    lines = [
        "# Langfuse Validation Summary",
        "",
        f"- Validation command exit code: `{cli_exit_code}`",
    ]

    if report_payload is None:
        lines.extend([
            "- Validation report: not available",
        ])
    else:
        lines.extend([
            f"- Validation all passed: `{report_payload.get('all_passed')}`",
            f"- Scenario count: `{len(report_payload.get('results', []))}`",
            "",
            "## Scenarios",
            "",
            "| Scenario | Expected | Actual | Passed | Tokens | Steps | Trace |",
            "| --- | --- | --- | --- | ---: | ---: | --- |",
        ])
        for item in report_payload.get("results", []):
            trace_url = item.get("trace_url") or ""
            trace_cell = f"[trace]({trace_url})" if trace_url else ""
            lines.append(
                "| "
                f"{item.get('scenario', '')} | "
                f"{item.get('expected_status', '')} | "
                f"{item.get('actual_status', '')} | "
                f"{'yes' if item.get('passed') else 'no'} | "
                f"{item.get('tokens', 0)} | "
                f"{item.get('steps', 0)} | "
                f"{trace_cell} |"
            )

    lines.extend(["", "## Baseline Comparison", ""])
    if comparison is None:
        lines.append(f"- Baseline comparison: skipped (`{compare_baseline_path or 'not provided'}`)")
    else:
        lines.extend([
            f"- Comparison passed: `{comparison.passed}`",
            f"- Baseline path: `{comparison.baseline_path}`",
            "",
            "| Check | Passed | Details |",
            "| --- | --- | --- |",
        ])
        for check in comparison.checks:
            lines.append(f"| `{check.name}` | {'yes' if check.passed else 'no'} | {check.details} |")

    return "\n".join(lines) + "\n"


def _compute_pass_rate(results: list[dict[str, Any]]) -> float:
    if not results:
        return 0.0
    return sum(1 for item in results if item.get("passed")) / len(results)


def _compute_average_tokens(results: list[dict[str, Any]]) -> float:
    if not results:
        return 0.0
    return sum(float(item.get("tokens", 0) or 0) for item in results) / len(results)
