"""Safe parent-owned integration and verification for delegation worktrees."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class IntegrationDecision:
    task_id: str
    action: str
    expected_revision: str

    def __post_init__(self) -> None:
        if self.action not in {"apply", "discard", "retain"}:
            raise ValueError("Integration action must be apply, discard, or retain")
        if not self.task_id.strip() or not self.expected_revision.strip():
            raise ValueError("Integration decisions require task_id and expected_revision")


class DelegationIntegrationCoordinator:
    """Integrate reviewed child worktrees, then verify the parent workspace."""

    def __init__(
        self,
        runtime: Any,
        store: Any,
        event_callback: Callable[[str, str, dict[str, object]], None] | None = None,
    ) -> None:
        self._runtime = runtime
        self._store = store
        self._event_callback = event_callback

    def integrate(
        self,
        *,
        parent_session_id: str,
        run_id: str,
        decisions: Iterable[IntegrationDecision],
    ) -> dict[str, object]:
        run = self._owned_run(parent_session_id, run_id)
        tasks = self._effective_tasks(run_id)
        decision_by_task = {item.task_id: item for item in decisions}
        pending = [
            task for task in tasks
            if str(task["integration_status"]) in {"pending", "retained"}
        ]
        missing = [str(task["id"]) for task in pending if str(task["id"]) not in decision_by_task]
        if missing:
            raise ValueError(
                "Every pending worktree requires an explicit reviewed decision: "
                + ", ".join(missing)
            )
        transitioned = self._store.transition_delegation_run(
            run_id,
            status="running",
            phase="integrating",
            expected_version=int(run["version"]),
        )
        if not transitioned:
            raise ValueError("Delegation run changed before integration could start")
        self._emit("delegation_integration_started", run_id, {
            "phase": "integrating",
            "status": "running",
            "task_count": len(pending),
        })

        outcomes: list[dict[str, object]] = []
        for task in self._topological(tasks):
            task_id = str(task["id"])
            decision = decision_by_task.get(task_id)
            if decision is None:
                continue
            child_id = str(task.get("child_session_id") or "")
            if not child_id:
                raise ValueError(f"Delegation task {task_id} has no child session")
            evidence = self._runtime.inspect_subagent_worktree(
                parent_session_id, child_id,
            )
            violation = self._write_set_violation(
                task.get("write_files", []), evidence.changed_files,
            )
            if violation:
                self._store.update_delegation_task(
                    task_id,
                    status=str(task["status"]),
                    integration_status="contract_violation",
                    integration_error=violation,
                )
                current = self._store.get_delegation_run(run_id) or {}
                verification = {"status": "not_run", "reason": violation}
                terminal_event = self._store.finalize_delegation_run(
                    run_id,
                    status="partial",
                    phase="integration_failed",
                    expected_version=int(current.get("version", 0)),
                    verification=verification,
                )
                self._emit("delegation_integration_completed", run_id, {
                    "phase": "integration_failed",
                    "status": "partial",
                    "integration_status": "contract_violation",
                    "error": violation,
                })
                self._broadcast_terminal(run_id, terminal_event)
                outcomes.append({
                    "task_id": task_id,
                    "status": "contract_violation",
                    "error": violation,
                    "changed_files": list(evidence.changed_files),
                })
                return self._result(run_id, outcomes)
            operation = self._resolve(
                parent_session_id,
                child_id,
                decision,
            )
            outcomes.append({
                "task_id": task_id,
                "child_session_id": child_id,
                "action": decision.action,
                "status": operation.status.value,
                "error": operation.error,
                "changed_files": list(evidence.changed_files),
            })
            if not operation.is_success:
                current = self._store.get_delegation_run(run_id) or {}
                verification = {
                    "status": "not_run",
                    "reason": operation.error or operation.status.value,
                }
                terminal_event = self._store.finalize_delegation_run(
                    run_id,
                    status="partial",
                    phase="integration_failed",
                    expected_version=int(current.get("version", 0)),
                    verification=verification,
                )
                self._emit("delegation_integration_completed", run_id, {
                    "phase": "integration_failed",
                    "status": "partial",
                    "integration_status": operation.status.value,
                    "error": operation.error,
                })
                self._broadcast_terminal(run_id, terminal_event)
                return self._result(run_id, outcomes)

        converged = self._store.reconcile_delegation_run(run_id)
        terminal_event = converged.pop("_terminal_event", None)
        self._emit("delegation_integration_completed", run_id, {
            "phase": str(converged.get("phase", "")),
            "status": str(converged.get("status", "")),
            "integration_status": "completed",
        })
        self._broadcast_terminal(run_id, terminal_event)
        if str(converged["status"]) == "running" and str(converged["phase"]) == "awaiting_verification":
            self.verify(parent_session_id=parent_session_id, run_id=run_id)
        return self._result(run_id, outcomes)

    def verify(self, *, parent_session_id: str, run_id: str) -> dict[str, object]:
        run = self._owned_run(parent_session_id, run_id)
        commands = self._verification_commands()
        if not commands:
            verification = {
                "status": "skipped",
                "reason": (
                    "No verification commands configured. Set "
                    "GRACE_MULTI_AGENT_VERIFICATION_COMMANDS to enable."
                ),
                "checks": [],
            }
            terminal_event = self._store.finalize_delegation_run(
                run_id,
                status="completed",
                phase="completed",
                expected_version=int(run["version"]),
                verification=verification,
            )
            self._emit("delegation_verification_completed", run_id, {
                "phase": "completed",
                "status": "skipped",
                "verification": verification,
            })
            self._broadcast_terminal(run_id, terminal_event)
            return self._store.get_delegation_run(run_id) or {}

        current = self._store.get_delegation_run(run_id) or run
        transitioned = self._store.transition_delegation_run(
            run_id,
            status="running",
            phase="verifying",
            expected_version=int(current["version"]),
        )
        if not transitioned:
            raise ValueError("Delegation run changed before verification could start")
        self._emit("delegation_verification_started", run_id, {
            "phase": "verifying", "status": "running",
        })
        from core.process import LocalRuntime

        repo_path = self._runtime.get_session_repo_path(parent_session_id)
        process = LocalRuntime(workspace_root=repo_path)
        timeout = self._verification_timeout()
        checks: list[dict[str, object]] = []
        passed = True
        for argv in commands:
            result = process.execute(
                argv[0], args=argv[1:], cwd=repo_path, timeout=timeout,
            )
            success = result.returncode == 0
            passed = passed and success
            checks.append({
                "command": argv,
                "status": "passed" if success else "failed",
                "returncode": result.returncode,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            })
            if not success:
                break
        current = self._store.get_delegation_run(run_id) or {}
        verification = {
            "status": "passed" if passed else "failed",
            "checks": checks,
        }
        terminal_event = self._store.finalize_delegation_run(
            run_id,
            status="completed" if passed else "partial",
            phase="completed" if passed else "verification_failed",
            expected_version=int(current.get("version", 0)),
            verification=verification,
        )
        self._emit("delegation_verification_completed", run_id, {
            "phase": "completed" if passed else "verification_failed",
            "status": "passed" if passed else "failed",
            "verification": verification,
        })
        self._broadcast_terminal(run_id, terminal_event)
        return self._store.get_delegation_run(run_id) or {}

    def _resolve(
        self,
        parent_session_id: str,
        child_session_id: str,
        decision: IntegrationDecision,
    ) -> Any:
        method = {
            "apply": self._runtime.apply_subagent_worktree,
            "discard": self._runtime.discard_subagent_worktree,
            "retain": self._runtime.retain_subagent_worktree,
        }[decision.action]
        return method(
            parent_session_id,
            child_session_id,
            expected_revision=decision.expected_revision,
        )

    def _owned_run(self, parent_session_id: str, run_id: str) -> dict[str, object]:
        run = self._store.get_delegation_run(run_id)
        if run is None or str(run["parent_session_id"]) != parent_session_id:
            raise ValueError("Delegation run is outside the parent session")
        return run

    def _effective_tasks(self, run_id: str) -> list[dict[str, object]]:
        return [
            task for task in self._store.list_delegation_tasks(run_id)
            if str(task["status"]) != "superseded"
        ]

    @staticmethod
    def _topological(tasks: list[dict[str, object]]) -> list[dict[str, object]]:
        by_id = {str(task["id"]): task for task in tasks}
        remaining = set(by_id)
        ordered: list[dict[str, object]] = []
        while remaining:
            ready = sorted(
                task_id for task_id in remaining
                if all(
                    str(dep) not in remaining
                    for dep in by_id[task_id].get("dependencies", [])
                    if str(dep) in by_id
                )
            )
            if not ready:
                raise ValueError("Delegation task graph contains a cycle")
            for task_id in ready:
                ordered.append(by_id[task_id])
                remaining.remove(task_id)
        return ordered

    @classmethod
    def _write_set_violation(
        cls,
        declared: Iterable[object],
        actual: Iterable[object],
    ) -> str:
        try:
            declared_paths = {cls._project_path(item) for item in declared}
            actual_paths = {cls._project_path(item) for item in actual}
        except ValueError as exc:
            return str(exc)
        if actual_paths and not declared_paths:
            return "Write worker changed files without declaring write_files"
        unexpected = sorted(actual_paths - declared_paths)
        if unexpected:
            return "Worker changed files outside declared write_files: " + ", ".join(unexpected)
        return ""

    @staticmethod
    def _project_path(value: object) -> str:
        raw = str(value).replace("\\", "/").strip()
        path = PurePosixPath(raw)
        if not raw or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Unsafe project-relative path: {raw!r}")
        return path.as_posix().removeprefix("./")

    @staticmethod
    def _verification_commands() -> list[list[str]]:
        raw = os.environ.get("GRACE_MULTI_AGENT_VERIFICATION_COMMANDS", "").strip()
        if not raw:
            return []
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "GRACE_MULTI_AGENT_VERIFICATION_COMMANDS must be valid JSON"
            ) from exc
        if not isinstance(value, list) or len(value) > 8:
            raise ValueError("Verification commands must be a JSON array of at most 8 argv arrays")
        commands: list[list[str]] = []
        for item in value:
            if (
                not isinstance(item, list)
                or not item
                or len(item) > 32
                or not all(isinstance(arg, str) and arg for arg in item)
            ):
                raise ValueError("Each verification command must be a non-empty argv array")
            commands.append(list(item))
        return commands

    @staticmethod
    def _verification_timeout() -> int:
        try:
            value = int(os.environ.get("GRACE_MULTI_AGENT_VERIFICATION_TIMEOUT", "300"))
        except ValueError:
            value = 300
        return max(1, min(value, 1800))

    def _emit(self, event_type: str, run_id: str, payload: dict[str, object]) -> None:
        if self._event_callback is not None:
            self._event_callback(event_type, run_id, payload)

    def _broadcast_terminal(
        self, run_id: str, terminal_event: object,
    ) -> None:
        """Live terminal delivery is owned by the durable OutboxRelay."""

    def _result(
        self, run_id: str, outcomes: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "run": self._store.get_delegation_run(run_id) or {},
            "tasks": self._store.list_delegation_tasks(run_id),
            "integration_outcomes": outcomes,
        }
