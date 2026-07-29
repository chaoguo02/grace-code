"""Durable ready-wave scheduler for retried and recovered delegation tasks."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from agent.session.models import (
    AgentRunResult,
    AgentRunStatus,
    ExplicitDelegationRequest,
    WorktreeDisposition,
)
from agent.session.result_contract import ChangedFile, WorkerReport, WorkerReportStatus
from agent.session.task_contract import TaskContract


class DelegationRunScheduler:
    """Execute queued effective tasks without creating a second run model."""

    TERMINAL = {
        "completed", "partial", "failed", "cancelled", "interrupted",
        "no_findings", "budget_exhausted", "rejected",
    }
    SUCCESS = {"completed", "no_findings"}

    def __init__(
        self,
        runtime: Any,
        store: Any,
        event_callback: Callable[[str, str, dict[str, object]], None] | None = None,
    ) -> None:
        self._runtime = runtime
        self._store = store
        self._event_callback = event_callback

    def execute(self, *, parent_session_id: str, run_id: str) -> dict[str, object]:
        run = self._store.get_delegation_run(run_id)
        if run is None or str(run["parent_session_id"]) != parent_session_id:
            raise ValueError("Delegation run is outside the parent session")
        while True:
            tasks = [
                task for task in self._store.list_delegation_tasks(run_id)
                if str(task["status"]) != "superseded"
            ]
            by_id = {str(task["id"]): task for task in tasks}
            queued = [task for task in tasks if str(task["status"]) == "queued"]
            if not queued:
                break
            blocked = []
            ready = []
            for task in queued:
                dependencies = [by_id.get(str(item)) for item in task["dependencies"]]
                if any(item is None for item in dependencies):
                    blocked.append((task, "Dependency record is unavailable"))
                elif any(
                    str(item["status"]) in self.TERMINAL
                    and str(item["status"]) not in self.SUCCESS
                    for item in dependencies
                    if item is not None
                ):
                    blocked.append((task, "Dependency incomplete"))
                elif all(
                    str(item["status"]) in self.SUCCESS
                    for item in dependencies
                    if item is not None
                ):
                    ready.append(task)
            for task, reason in blocked:
                self._store.update_delegation_task(
                    str(task["id"]),
                    status="failed",
                    error=reason,
                    report={
                        "task_id": str(task["id"]),
                        "status": "failed",
                        "summary": reason,
                        "unresolved": [reason],
                    },
                    expected_statuses=("queued",),
                )
                self._emit("delegation_task_blocked", run_id, {
                    "task_id": str(task["id"]), "reason": reason,
                })
            if not ready:
                if blocked:
                    continue
                raise ValueError("Delegation task graph has no ready tasks")
            from agent.session.multi_agent_config import MultiAgentFeatureConfig

            config = MultiAgentFeatureConfig.from_environment()
            wave_limit = min(config.max_wave_fanout, config.max_concurrent)
            for offset in range(0, len(ready), wave_limit):
                wave = ready[offset:offset + wave_limit]
                with ThreadPoolExecutor(
                    max_workers=min(len(wave), config.max_concurrent),
                    thread_name_prefix="delegation-resume",
                ) as executor:
                    futures = {
                        executor.submit(
                            self._execute_one, parent_session_id, run_id, task, by_id,
                        ): task
                        for task in wave
                    }
                    for future in as_completed(futures):
                        future.result()
        converged = self._store.reconcile_delegation_run(run_id)
        terminal_event = converged.pop("_terminal_event", None)
        if isinstance(terminal_event, dict):
            self._emit(
                "delegation_completed", run_id,
                {"_persisted_event": terminal_event},
            )
        else:
            self._emit("delegation_phase_changed", run_id, {
                "phase": str(converged.get("phase", "")),
                "status": str(converged.get("status", "")),
            })
        return converged

    def _execute_one(
        self,
        parent_session_id: str,
        run_id: str,
        task: dict[str, object],
        by_id: dict[str, dict[str, object]],
    ) -> WorkerReport:
        task_id = str(task["id"])
        definition = self._runtime.agent_registry.get(str(task["agent_type"]))
        parent = self._store.get_session(parent_session_id)
        if parent is None:
            raise ValueError(f"Unknown parent session: {parent_session_id}")
        parent_definition = self._runtime.agent_registry.get(parent.agent_name)
        dependency_context = "\n".join(
            f"- {dep}: {((by_id[dep].get('report') or {}).get('summary', 'completed'))}"
            for dep in task["dependencies"]
            if dep in by_id
        )
        prompt = (
            f"OBJECTIVE\n{task['goal']}\n\n"
            f"TASK\n{task['prompt']}\n\n"
            f"SCOPE\n{task['scope']}\n\n"
            f"EXPECTED FILES\n{task['expected_files']}\n\n"
            f"WRITE FILES\n{task['write_files']}\n\n"
            f"DEPENDENCY RESULTS\n{dependency_context or 'None'}\n\n"
            "This is a resumed durable delegation task. Return a standalone "
            "evidence-backed result and report unresolved work explicitly."
        )
        generation = 0

        def created(child: Any) -> None:
            nonlocal generation
            generation = int(child.generation)
            started = self._store.update_delegation_task(
                task_id,
                status="running",
                child_session_id=child.id,
                generation=generation,
                expected_statuses=("queued",),
            )
            if started:
                self._emit("delegation_task_started", run_id, {
                    "task_id": task_id,
                    "child_session_id": child.id,
                    "agent_type": definition.name,
                })

        contract = TaskContract.for_subagent(
            definition,
            self._runtime._root_agent_config,
            parent_budget_tokens=min(
                self._runtime._root_agent_config.budget_tokens,
                definition.max_tokens
                or self._runtime._root_agent_config.budget_tokens,
            ),
            parent_max_steps=self._runtime._root_agent_config.max_steps,
        )
        started_at = time.monotonic()
        try:
            result = self._runtime.run_explicit_delegation(
                parent_session_id,
                request=ExplicitDelegationRequest(
                    agent_name=definition.name,
                    description=str(task["goal"])[:80],
                    prompt=prompt,
                ),
                parent_intent=parent_definition.intent,
                contract=contract,
                child_metadata={
                    "delegation_run_id": run_id,
                    "delegation_task_id": task_id,
                    "retry_of": task.get("supersedes_task_id"),
                },
                child_created_callback=created,
            )
            if not isinstance(result, AgentRunResult):
                raise TypeError("Resumed delegation returned no AgentRunResult")
            report = self._report(
                task, result, generation,
                int((time.monotonic() - started_at) * 1000),
            )
            integration_status = (
                "pending"
                if result.worktree_disposition is WorktreeDisposition.PRESERVED
                else "not_required"
            )
            persisted = self._store.update_delegation_task(
                task_id,
                status=report.status.value,
                child_session_id=result.session_id,
                generation=generation,
                report=report.to_dict(),
                error=result.error,
                integration_status=integration_status,
                expected_statuses=("running",),
            )
            if persisted:
                self._emit("delegation_task_reported", run_id, {
                    "task_id": task_id,
                    "child_session_id": result.session_id,
                    "agent_type": definition.name,
                    "status": report.status.value,
                    "tokens_used": report.tokens_used,
                    "duration_ms": report.duration_ms,
                })
            return report
        except Exception as exc:
            report = WorkerReport(
                task_id=task_id,
                session_id=f"failed:{task_id}",
                generation=generation,
                agent_type=definition.name,
                status=WorkerReportStatus.FAILED,
                summary=f"Worker failed: {exc}",
                unresolved=(str(exc),),
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            self._store.update_delegation_task(
                task_id,
                status="failed",
                report=report.to_dict(),
                error=str(exc),
                expected_statuses=("queued", "running"),
            )
            self._emit("delegation_task_failed", run_id, {
                "task_id": task_id,
                "agent_type": definition.name,
                "status": "failed",
            })
            return report

    @staticmethod
    def _report(
        task: dict[str, object],
        result: AgentRunResult,
        generation: int,
        duration_ms: int,
    ) -> WorkerReport:
        status = {
            AgentRunStatus.COMPLETED: WorkerReportStatus.COMPLETED,
            AgentRunStatus.PARTIAL: WorkerReportStatus.PARTIAL,
            AgentRunStatus.FAILED: WorkerReportStatus.FAILED,
            AgentRunStatus.CANCELLED: WorkerReportStatus.CANCELLED,
        }[result.status]
        evidence = result.worktree
        return WorkerReport(
            task_id=str(task["id"]),
            session_id=result.session_id,
            generation=generation,
            agent_type=str(task["agent_type"]),
            status=status,
            summary=result.summary,
            findings=result.structured_findings,
            changed_files=tuple(
                ChangedFile(path=path)
                for path in (evidence.changed_files if evidence is not None else ())
            ),
            unresolved=(result.error,) if result.error else (),
            warnings=tuple(
                value for value in (result.warning, result.failure_diagnosis) if value
            ),
            tokens_used=result.tokens_used,
            duration_ms=duration_ms,
            worktree=evidence.to_dict() if evidence is not None else None,
        )

    @staticmethod
    def _max_concurrent() -> int:
        try:
            configured = int(os.environ.get("GRACE_MAX_CONCURRENT_SUBAGENTS", "4"))
        except ValueError:
            configured = 4
        return max(1, min(configured, 16))

    def _emit(self, event_type: str, run_id: str, payload: dict[str, object]) -> None:
        if self._event_callback is not None:
            self._event_callback(event_type, run_id, payload)
