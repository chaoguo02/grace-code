"""Read-only aggregation of local evaluation and regression artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from core.state_paths import ProjectStatePaths
from observability.ci import compare_validation_report_to_baseline
from observability.validation import get_langfuse_validation_scenarios

logger = logging.getLogger(__name__)

_MAX_JSON_BYTES = 10 * 1024 * 1024
_MAX_RUNS = 100


class EvaluationService:
    """Expose existing CLI/CI evaluation facts without starting evaluations."""

    def __init__(self, repo_path: str) -> None:
        self._repo_path = Path(repo_path).resolve()

    def get_overview(self) -> dict[str, Any]:
        baselines = self._load_baselines()
        preferred_baseline = baselines[0] if baselines else None
        runs = self._load_runs(preferred_baseline)
        latest = runs[0] if runs else None
        previous = runs[1] if len(runs) > 1 else None
        return {
            "scenario_catalog": [
                {
                    "name": scenario.name,
                    "description": scenario.description,
                    "expected_status": scenario.expected_status,
                    "max_steps": scenario.max_steps,
                    "budget_tokens": scenario.budget_tokens,
                    "intent": scenario.intent,
                    "mode": scenario.mode,
                    "expect_failure_dataset_increment": (
                        scenario.expect_failure_dataset_increment
                    ),
                }
                for scenario in get_langfuse_validation_scenarios()
            ],
            "runs": runs,
            "baselines": [
                {
                    key: value
                    for key, value in baseline.items()
                    if key != "payload"
                }
                for baseline in baselines
            ],
            "summary": self._build_summary(latest, previous, runs),
            "domain_gates": self._build_domain_gates(latest),
            "disclosure": {
                "source": "langfuse_validation_artifacts",
                "read_only": True,
                "session_completion_is_not_a_pass": True,
                "auto_run_enabled": False,
            },
        }

    def _build_domain_gates(
        self, latest: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """Build eight-domain completion strictly from inspectable evidence."""
        suites = {
            "offline_evaluation": [
                "pyproject.toml", "tests/test_evaluation_service.py",
                "web/package.json", "web/playwright.config.ts",
            ],
            "replay": [
                "server/services/replay_service.py", "server/routers/replay.py",
                "web/src/components/ReplayLab.tsx", "tests/test_replay_service.py",
            ],
            "tool_retry": [
                "core/tool_execution.py", "core/types.py",
                "tests/test_tool_execution_pipeline.py",
            ],
            "memory": [
                "memory/sqlite_backend.py", "memory/recall.py",
                "tests/test_memory_recall.py",
            ],
            "security": [
                "hitl/pipeline.py", "server/main.py",
                "tests/test_server_bind_security.py",
            ],
            "state_machine": [
                "agent/session/task_state_machine.py",
                "tests/test_react_turn_seams.py",
            ],
            "long_context": [
                "context/compaction.py", "agent/session/session_store.py",
                "tests/test_compaction_trigger.py",
            ],
            "observability": [
                "server/services/stats_recorder.py",
                "web/src/components/ReliabilityDashboard.tsx",
                "tests/test_evaluation_service.py",
            ],
        }
        result = []
        for domain, paths in suites.items():
            checks = [
                {
                    "id": path,
                    "passed": (self._repo_path / path).is_file(),
                    "evidence": path,
                }
                for path in paths
            ]
            checks.append({
                "id": "latest_deterministic_gate",
                "passed": bool(latest and latest.get("all_passed")),
                "evidence": latest.get("path", "") if latest else "No CI artifact",
            })
            passed = sum(1 for check in checks if check["passed"])
            result.append({
                "domain": domain,
                "passed": passed,
                "total": len(checks),
                "completion": passed / len(checks) if checks else 0.0,
                "status": "passed" if passed == len(checks) else "incomplete",
                "checks": checks,
                "last_success_commit": (
                    str((latest or {}).get("configuration", {}).get("commit", ""))
                ),
                "last_run_at": (latest or {}).get("created_at", ""),
            })
        return result

    def _load_runs(
        self,
        preferred_baseline: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        candidates: list[Path] = []
        for root in self._run_roots():
            if not root.is_dir():
                continue
            try:
                candidates.extend(root.glob("*/validation-report.json"))
            except OSError:
                logger.debug("Unable to scan evaluation root %s", root)
        unique = {
            str(path.resolve()): path
            for path in candidates
            if path.is_file()
        }
        ordered = sorted(
            unique.values(),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:_MAX_RUNS]
        runs: list[dict[str, Any]] = []
        for report_path in ordered:
            payload = self._read_json(report_path)
            if payload is None:
                continue
            run = self._normalize_run(report_path, payload, preferred_baseline)
            if run is not None:
                runs.append(run)
        return runs

    def _normalize_run(
        self,
        report_path: Path,
        payload: dict[str, Any],
        preferred_baseline: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            return None
        results = [
            dict(item)
            for item in raw_results
            if isinstance(item, dict) and item.get("scenario")
        ]
        passed_count = sum(1 for result in results if result.get("passed"))
        tokens = [int(result.get("tokens", 0) or 0) for result in results]
        steps = [int(result.get("steps", 0) or 0) for result in results]
        run_dir = report_path.parent
        created_at = self._artifact_created_at(run_dir, report_path)

        generated_baseline = self._read_json(run_dir / "baseline.json")
        configuration = self._baseline_configuration(generated_baseline or {})
        comparison_payload = self._read_json(run_dir / "comparison.json")
        comparison_source = "artifact" if comparison_payload is not None else ""
        if comparison_payload is None and preferred_baseline is not None:
            comparison = compare_validation_report_to_baseline(
                payload,
                preferred_baseline["payload"],
                report_path=self._display_path(report_path),
                baseline_path=preferred_baseline["path"],
            )
            comparison_payload = comparison.to_dict()
            comparison_source = "computed"
        run_id = hashlib.sha256(
            str(report_path.resolve()).encode("utf-8")
        ).hexdigest()[:12]

        return {
            "id": f"{run_dir.name}-{run_id}",
            "label": run_dir.name,
            "created_at": created_at,
            "path": self._display_path(report_path),
            "all_passed": bool(
                payload.get(
                    "all_passed",
                    bool(results) and passed_count == len(results),
                )
            ),
            "pass_rate": passed_count / len(results) if results else 0.0,
            "passed_count": passed_count,
            "scenario_count": len(results),
            "average_tokens": sum(tokens) / len(tokens) if tokens else 0.0,
            "total_tokens": sum(tokens),
            "average_steps": sum(steps) / len(steps) if steps else 0.0,
            "results": results,
            "configuration": configuration,
            "comparison": comparison_payload,
            "comparison_source": comparison_source,
        }

    def _load_baselines(self) -> list[dict[str, Any]]:
        candidates: list[Path] = []
        for root in self._baseline_roots():
            if not root.is_dir():
                continue
            try:
                candidates.extend(root.glob("*.json"))
            except OSError:
                logger.debug("Unable to scan baseline root %s", root)
        unique = {
            str(path.resolve()): path
            for path in candidates
            if path.is_file()
        }
        baselines: list[dict[str, Any]] = []
        for path in unique.values():
            payload = self._read_json(path)
            if payload is None or not isinstance(payload.get("results"), list):
                continue
            results = [
                item for item in payload["results"] if isinstance(item, dict)
            ]
            pass_rate = float(payload.get(
                "pass_rate",
                (
                    sum(1 for item in results if item.get("passed"))
                    / len(results)
                    if results else 0.0
                ),
            ))
            baselines.append({
                "id": path.stem,
                "name": str(payload.get("baseline_name") or path.stem),
                "created_at": str(
                    payload.get("created_at")
                    or datetime.fromtimestamp(
                        path.stat().st_mtime,
                        tz=timezone.utc,
                    ).isoformat()
                ),
                "path": self._display_path(path),
                "pass_rate": pass_rate,
                "average_tokens": float(payload.get("average_tokens", 0) or 0),
                "scenario_count": len(results),
                "configuration": self._baseline_configuration(payload),
                "payload": payload,
            })
        baselines.sort(
            key=lambda baseline: baseline["created_at"],
            reverse=True,
        )
        return baselines

    @staticmethod
    def _baseline_configuration(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider": str(payload.get("provider") or ""),
            "model": str(payload.get("model") or ""),
            "prompt_source": str(payload.get("prompt_source") or ""),
            "prompt_label": str(payload.get("prompt_label") or ""),
            "prompt_version": payload.get("prompt_version"),
        }

    @staticmethod
    def _build_summary(
        latest: dict[str, Any] | None,
        previous: dict[str, Any] | None,
        runs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if latest is None:
            return {
                "run_count": 0,
                "latest_pass_rate": 0.0,
                "latest_average_tokens": 0.0,
                "pass_rate_delta": None,
                "token_delta_pct": None,
                "regression_count": 0,
            }
        checks = (latest.get("comparison") or {}).get("checks", [])
        regression_count = sum(
            1 for check in checks
            if isinstance(check, dict) and not check.get("passed")
        )
        pass_delta = (
            latest["pass_rate"] - previous["pass_rate"]
            if previous is not None else None
        )
        token_delta = None
        if previous is not None and previous["average_tokens"] > 0:
            token_delta = (
                latest["average_tokens"] - previous["average_tokens"]
            ) / previous["average_tokens"]
        return {
            "run_count": len(runs),
            "latest_pass_rate": latest["pass_rate"],
            "latest_average_tokens": latest["average_tokens"],
            "pass_rate_delta": pass_delta,
            "token_delta_pct": token_delta,
            "regression_count": regression_count,
        }

    def _run_roots(self) -> list[Path]:
        return [
            self._repo_path / ".grace" / "ci" / "langfuse",
            self._repo_path / ".forge-agent" / "ci" / "langfuse",
        ]

    def _baseline_roots(self) -> list[Path]:
        roots = [self._repo_path / "ci" / "langfuse-baselines"]
        try:
            roots.append(
                ProjectStatePaths.for_project(self._repo_path).experiments
                / "langfuse-baselines"
            )
        except (OSError, ValueError):
            logger.debug("Isolated evaluation baseline root unavailable")
        return roots

    @staticmethod
    def _artifact_created_at(run_dir: Path, report_path: Path) -> str:
        raw = run_dir.name
        try:
            parsed = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc,
            )
            return parsed.isoformat()
        except ValueError:
            return datetime.fromtimestamp(
                report_path.stat().st_mtime,
                tz=timezone.utc,
            ).isoformat()

    def _display_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self._repo_path).as_posix()
        except ValueError:
            return path.name

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            if not path.is_file() or path.stat().st_size > _MAX_JSON_BYTES:
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
        except (OSError, UnicodeError, json.JSONDecodeError):
            logger.debug("Unable to read evaluation artifact %s", path)
            return None
