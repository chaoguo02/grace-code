"""Runtime-bound fan-out/fan-in and dependency-chain delegation tool."""

from __future__ import annotations

import copy
import json
import time
import uuid
from concurrent.futures import as_completed
from dataclasses import dataclass
from typing import Any

from agent.session.models import (
    AgentRunResult,
    AgentRunStatus,
    AgentSpawnRequest,
    ExecutionPlacement,
    WorkspaceMode,
    WorktreeDisposition,
)
from agent.session.executor_pool import borrowed_executor
from agent.session.result_contract import (
    ChangedFile,
    WorkerReport,
    WorkerReportStatus,
)
from agent.task import TaskIntent
from config.env import SubagentSafetyLimits
from core.base import (
    BaseTool,
    ToolConcurrency,
    ToolEffect,
    ToolMetadata,
    ToolResult,
    ToolRole,
)


@dataclass(frozen=True)
class _BatchTask:
    id: str
    agent_type: str
    goal: str
    prompt: str
    purpose: str
    scope: tuple[str, ...]
    dependencies: tuple[str, ...]
    expected_files: tuple[str, ...]
    write_files: tuple[str, ...]
    required: bool
    model: str | None
    isolation: WorkspaceMode | None


class AgentBatchTool(BaseTool):
    """Execute a validated worker DAG with real child sessions and fan-in."""

    aliases = ("Workflow",)

    def __init__(
        self,
        runtime: Any,
        parent_session_id: str,
        *,
        caller_agent_name: str,
    ) -> None:
        self._runtime = runtime
        self._parent_session_id = parent_session_id
        self._caller_agent_name = caller_agent_name
        self._run_context = None
        caller = runtime.agent_registry.get(caller_agent_name)
        effect = (
            ToolEffect.DELEGATE_READ_ONLY
            if caller.intent is TaskIntent.ANALYSIS
            else ToolEffect.DELEGATE_WRITE
        )
        self.metadata = ToolMetadata(
            effects=frozenset({effect}),
            roles=frozenset({ToolRole.DELEGATE}),
        )

    def with_run_context(self, context: Any) -> "AgentBatchTool":
        from agent.session.run_context import RunContext

        if not isinstance(context, RunContext):
            raise TypeError("AgentBatch requires a Runtime-bound RunContext")
        bound = copy.copy(self)
        bound._run_context = context
        return bound

    @property
    def name(self) -> str:
        return "AgentBatch"

    @property
    def description(self) -> str:
        return (
            "Run a bounded DAG of 2 or more well-scoped subagent tasks as a real "
            "persisted workflow. Independent safe tasks execute in bounded waves; "
            "dependencies execute later; all reports fan back into one result. "
            "Use Agent for one worker."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        allowed = [
            item.name
            for item in self._runtime.agent_registry.delegatable_by(
                self._runtime.agent_registry.get(self._caller_agent_name)
            )
        ]
        task = {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "agent": {"type": "string", "enum": allowed},
                "goal": {"type": "string"},
                "prompt": {"type": "string"},
                "purpose": {"type": "string"},
                "scope": {"type": "array", "items": {"type": "string"}},
                "depends_on": {"type": "array", "items": {"type": "string"}},
                "expected_files": {
                    "type": "array", "items": {"type": "string"},
                },
                "write_files": {
                    "type": "array", "items": {"type": "string"},
                },
                "required": {"type": "boolean"},
                "model": {"type": "string"},
                "isolation": {
                    "type": "string",
                    "enum": ["current", "worktree"],
                },
                "acceptance": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Task-level acceptance criteria.",
                },
            },
            "required": ["id", "goal", "prompt"],
        }
        config = self._feature_config()
        return {
            "type": "object",
            "properties": {
                "schema_version": {"type": "integer", "default": 1},
                "description": {"type": "string"},
                "reason_code": {"type": "string"},
                "explanation": {"type": "string"},
                "acceptance": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Run-level acceptance criteria.",
                },
                "topology": {
                    "type": "string",
                    "enum": ["fan_out_fan_in", "chain"],
                },
                "tasks": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": config.max_tasks,
                    "items": task,
                },
            },
            "required": ["description", "tasks"],
        }

    def concurrency_mode(self, params: dict[str, Any]) -> ToolConcurrency:
        return ToolConcurrency.SERIAL

    def execute(self, params: dict[str, Any]) -> ToolResult:
        if not self._feature_config().enabled:
            return ToolResult(
                success=False,
                output="",
                error=(
                    "Multi-Agent mode is disabled by "
                    "GRACE_MULTI_AGENT_MODE_ENABLED"
                ),
            )
        if self._run_context is None:
            return ToolResult(
                success=False,
                output="",
                error="AgentBatch requires a Runtime-bound run context",
            )
        if (
            self._run_context.phase_policy is None
            or self._run_context.delegation_effects is None
            or self._run_context.delegation_step_limit is None
        ):
            return ToolResult(
                success=False,
                output="",
                error="AgentBatch requires delegation policy and step limits",
            )
        if self._run_context.cancellation.is_cancelled:
            return ToolResult(
                success=False,
                output="",
                error=self._run_context.cancellation.detail,
            )
        try:
            tasks = self._parse_tasks(params.get("tasks"))
            requested_topology = str(
                params.get("topology") or self._infer_topology(tasks)
            )
            self._validate(tasks, requested_topology)
            decision = self._plan_topology(tasks, requested_topology)
            topology = decision.topology.value
            if topology not in {"fan_out_fan_in", "chain"}:
                return ToolResult(
                    success=False,
                    output="",
                    error=(
                        f"AgentBatch downgraded to {topology}: "
                        f"{decision.explanation}. Use Agent or complete directly."
                    ),
                    metadata={"routing_decision": {
                        "topology": topology,
                        "reason_code": decision.reason_code,
                        "explanation": decision.explanation,
                        "downgraded_from": (
                            decision.downgraded_from.value
                            if decision.downgraded_from else None
                        ),
                    }},
                )
        except (TypeError, ValueError) as exc:
            return ToolResult(success=False, output="", error=str(exc))

        run_context = self._run_context
        run_id = f"delegation-{uuid.uuid4().hex}"
        store = self._runtime._store
        budget = {
            "available_tokens": decision.estimated_budget.available_tokens,
            "parent_reserve_tokens": (
                decision.estimated_budget.parent_reserve_tokens
            ),
            "recovery_reserve_tokens": (
                decision.estimated_budget.recovery_reserve_tokens
            ),
            "worker_pool_tokens": decision.estimated_budget.worker_tokens,
            "max_concurrent": self._max_concurrent(runtime=self._runtime),
        }
        store.create_delegation_run(
            run_id=run_id,
            parent_session_id=self._parent_session_id,
            parent_run_id=str(getattr(run_context, "run_id", "") or ""),
            topology=topology,
            reason_code=str(params.get("reason_code") or decision.reason_code),
            explanation=str(
                params.get("explanation")
                or decision.explanation
            ),
            budget=budget,
            downgraded_from=(
                decision.downgraded_from.value
                if decision.downgraded_from else None
            ),
        )
        self._emit(
            "delegation_planned",
            run_id,
            {
                "topology": topology,
                "reason_code": str(
                    params.get("reason_code") or decision.reason_code
                ),
                "task_count": len(tasks),
                "budget": budget,
            },
        )
        for task in tasks:
            store.create_delegation_task(
                task_id=f"{run_id}:{task.id}",
                delegation_run_id=run_id,
                agent_type=task.agent_type,
                purpose=task.purpose,
                goal=task.goal,
                prompt=task.prompt,
                scope=task.scope,
                dependencies=tuple(
                    f"{run_id}:{item}" for item in task.dependencies
                ),
                expected_files=task.expected_files,
                write_files=task.write_files,
                required=task.required,
            )
            self._emit(
                "delegation_task_queued",
                run_id,
                {"task_id": task.id, "agent_type": task.agent_type},
            )

        reports: dict[str, WorkerReport] = {}
        pending = {task.id: task for task in tasks}
        failed_required = False
        incomplete = {
            WorkerReportStatus.PARTIAL,
            WorkerReportStatus.FAILED,
            WorkerReportStatus.CANCELLED,
        }
        try:
            while pending:
                if run_context.cancellation.is_cancelled:
                    detail = run_context.cancellation.detail or "Delegation cancelled"
                    for task in tuple(pending.values()):
                        report = WorkerReport(
                            task_id=task.id,
                            session_id=f"cancelled:{task.id}",
                            generation=0,
                            agent_type=task.agent_type,
                            status=WorkerReportStatus.CANCELLED,
                            summary=detail,
                            unresolved=(detail,),
                        )
                        reports[task.id] = report
                        pending.pop(task.id)
                        self._persist_report(run_id, task, report, detail)
                        failed_required = failed_required or task.required
                    break
                ready = [
                    task
                    for task in pending.values()
                    if all(dep in reports for dep in task.dependencies)
                ]
                if not ready:
                    raise ValueError("Delegation task graph contains a cycle")
                blocked = [
                    task for task in ready
                    if any(reports[dep].status in incomplete for dep in task.dependencies)
                ]
                for task in blocked:
                    report = WorkerReport(
                        task_id=task.id,
                        session_id=f"blocked:{task.id}",
                        generation=0,
                        agent_type=task.agent_type,
                        status=WorkerReportStatus.FAILED,
                        summary="Dependency incomplete",
                        unresolved=("A dependency did not complete successfully",),
                    )
                    reports[task.id] = report
                    pending.pop(task.id)
                    self._persist_report(run_id, task, report, "Dependency incomplete")
                    failed_required = failed_required or task.required
                ready = [task for task in ready if task not in blocked]
                if not ready:
                    continue

                safe = [task for task in ready if self._is_parallel_safe(task)]
                serial = [task for task in ready if task not in safe]
                if safe:
                    config = self._feature_config()
                    effective_concurrency = max(
                        1, self._max_concurrent(runtime=self._runtime),
                    )
                    wave_limit = min(
                        config.max_wave_fanout,
                        effective_concurrency,
                    )
                    for offset in range(0, len(safe), wave_limit):
                        wave = safe[offset:offset + wave_limit]
                        if run_context.cancellation.is_cancelled:
                            break
                        with borrowed_executor(
                            self._runtime,
                            max_workers=min(
                                len(wave), effective_concurrency,
                            ),
                            thread_name_prefix="agent-batch",
                        ) as executor:
                            future_map = {
                                executor.submit(
                                    self._execute_one, run_id, task, reports
                                ): task
                                for task in wave
                            }
                            for future in as_completed(future_map):
                                task = future_map[future]
                                report = future.result()
                                reports[task.id] = report
                                pending.pop(task.id)
                                failed_required = (
                                    failed_required
                                    or task.required and report.status in incomplete
                                )
                for task in serial:
                    if run_context.cancellation.is_cancelled:
                        break
                    report = self._execute_one(run_id, task, reports)
                    reports[task.id] = report
                    pending.pop(task.id)
                    failed_required = (
                        failed_required
                        or task.required and report.status in incomplete
                    )
        except Exception as exc:
            current = store.get_delegation_run(run_id) or {}
            terminal_event = store.finalize_delegation_run(
                run_id,
                status="failed",
                phase="failed",
                expected_version=int(current.get("version", 0)),
                report_count=len(reports),
            )
            if terminal_event is not None:
                self._emit(
                    "delegation_completed", run_id,
                    {"_persisted_event": terminal_event},
                )
            return ToolResult(
                success=False,
                output="",
                error=f"AgentBatch failed: {exc}",
                metadata={
                    "delegation_run_id": run_id,
                    "worker_reports": [
                        item.to_dict() for item in reports.values()
                    ],
                },
            )

        persisted_before_synthesis = store.get_delegation_run(run_id) or {}
        if (
            str(persisted_before_synthesis.get("status")) == "cancelled"
            or run_context.cancellation.is_cancelled
        ):
            terminal_event = None
            if str(persisted_before_synthesis.get("status")) != "cancelled":
                terminal_event = store.finalize_delegation_run(
                    run_id,
                    status="cancelled",
                    phase="cancelled",
                    expected_version=int(persisted_before_synthesis.get("version", 0)),
                    report_count=len(reports),
                )
            persisted_run = store.get_delegation_run(run_id) or {}
            if terminal_event is not None:
                persisted_run["_terminal_event"] = terminal_event
        else:
            self._emit(
                "delegation_synthesis_started",
                run_id,
                {"report_count": len(reports)},
            )
            store.transition_delegation_run(
                run_id,
                status="running",
                phase="synthesizing",
                expected_statuses=("running",),
                synthesis={
                    "report_count": len(reports),
                    "required_incomplete": failed_required,
                },
            )
            persisted_run = store.reconcile_delegation_run(run_id)
        terminal_event = persisted_run.pop("_terminal_event", None)
        final_status = str(persisted_run.get("status", "failed"))
        final_phase = str(persisted_run.get("phase", final_status))
        if isinstance(terminal_event, dict):
            self._emit(
                "delegation_completed",
                run_id,
                {"_persisted_event": terminal_event},
            )
        ordered = [reports[task.id] for task in tasks]
        output = self._format_result(run_id, topology, ordered)
        success = final_status == "completed" and not failed_required
        if final_phase == "awaiting_integration":
            error = "Subagent worktrees require explicit integration review"
        elif failed_required:
            error = "One or more required subagent tasks were incomplete"
        elif not success:
            error = f"Delegation did not converge: {final_phase}"
        else:
            error = ""
        return ToolResult(
            success=success,
            output=output,
            error=error,
            subagent_tokens_used=sum(
                item.tokens_used
                for item in ordered
                if not item.budget_settled
            ),
            structured_findings=tuple(
                finding.to_dict()
                for report in ordered
                for finding in report.findings
            ),
            metadata={
                "delegation_run_id": run_id,
                "topology": topology,
                "phase": final_phase,
                "worker_reports": [item.to_dict() for item in ordered],
            },
        )

    def _execute_one(
        self,
        run_id: str,
        task: _BatchTask,
        prior_reports: dict[str, WorkerReport],
    ) -> WorkerReport:
        store_task_id = f"{run_id}:{task.id}"
        started = time.monotonic()
        child_identity: dict[str, Any] = {}
        persisted = self._runtime._store.get_delegation_task(store_task_id)
        if persisted is not None and str(persisted["status"]) == "cancelled":
            return WorkerReport(
                task_id=task.id,
                session_id=f"cancelled:{task.id}",
                generation=int(persisted.get("generation", 0)),
                agent_type=task.agent_type,
                status=WorkerReportStatus.CANCELLED,
                summary="Cancelled before worker start",
                unresolved=("User cancelled this queued task",),
                duration_ms=0,
            )

        def child_created(child: Any) -> None:
            child_identity["id"] = child.id
            child_identity["generation"] = int(child.generation)
            started = self._runtime._store.update_delegation_task(
                store_task_id,
                status="running",
                child_session_id=child.id,
                generation=int(child.generation),
                expected_statuses=("queued",),
            )
            if not started:
                return
            self._emit(
                "delegation_task_started",
                run_id,
                {
                    "task_id": task.id,
                    "child_session_id": child.id,
                    "agent_type": task.agent_type,
                },
            )

        definition = self._runtime.agent_registry.get(task.agent_type)
        if task.isolation is not None and task.isolation is not definition.workspace_mode:
            from dataclasses import replace

            definition = replace(definition, workspace_mode=task.isolation)
        dependency_context = "\n".join(
            f"- {dep}: {prior_reports[dep].summary}"
            for dep in task.dependencies
        )
        prompt = (
            f"OBJECTIVE\n{task.goal}\n\n"
            f"TASK\n{task.prompt}\n\n"
            f"SCOPE\n{json.dumps(task.scope, ensure_ascii=False)}\n\n"
            f"EXPECTED FILES\n"
            f"{json.dumps(task.expected_files, ensure_ascii=False)}\n\n"
            f"WRITE FILES\n{json.dumps(task.write_files, ensure_ascii=False)}\n\n"
            f"DEPENDENCY RESULTS\n{dependency_context or 'None'}\n\n"
            "DELIVERABLE\nReturn a standalone evidence-backed summary. "
            "Report partial completion and unresolved items explicitly."
        )
        request = AgentSpawnRequest.named(
            definition=definition,
            description=task.goal[:80],
            prompt=prompt,
            execution_placement=ExecutionPlacement.FOREGROUND,
            model_name=task.model,
        )
        run_context = self._run_context
        try:
            result = self._runtime.spawn_agent(
                parent_session_id=self._parent_session_id,
                request=request,
                budget_tokens=max(
                    1,
                    int(run_context.budget.token_remaining * 0.65)
                    // max(1, len(self._runtime._store.list_delegation_tasks(run_id))),
                ),
                parent_max_steps=run_context.delegation_step_limit,
                cancellation_token=run_context.cancellation,
                mode_policy=getattr(run_context, "mode_policy", None),
                evidence_store=getattr(run_context, "evidence_store", None),
                evidence_scope=getattr(run_context, "evidence_scope", None),
                parent_policy=run_context.phase_policy.with_allowed_effects(
                    run_context.delegation_effects
                ),
                spawn_context=run_context.spawn_context,
                child_metadata={
                    "delegation_run_id": run_id,
                    "delegation_task_id": store_task_id,
                    "purpose": task.purpose,
                    "required": task.required,
                    "expected_files": list(task.expected_files),
                    "write_files": list(task.write_files),
                },
                child_created_callback=child_created,
                parent_budget=run_context.budget,
            )
            if not isinstance(result, AgentRunResult):
                raise TypeError("AgentBatch foreground worker returned no result")
            report = self._worker_report(
                task,
                result,
                generation=int(child_identity.get("generation", 0)),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            integration_status = (
                "pending"
                if result.worktree_disposition is WorktreeDisposition.PRESERVED
                else "not_required"
            )
            self._persist_report(
                run_id,
                task,
                report,
                result.error,
                integration_status=integration_status,
            )
            return report
        except Exception as exc:
            report = WorkerReport(
                task_id=task.id,
                session_id=str(child_identity.get("id") or f"failed:{task.id}"),
                generation=int(child_identity.get("generation", 0)),
                agent_type=task.agent_type,
                status=WorkerReportStatus.FAILED,
                summary=f"Worker failed: {exc}",
                unresolved=(str(exc),),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            self._persist_report(run_id, task, report, str(exc))
            return report

    def _persist_report(
        self,
        run_id: str,
        task: _BatchTask,
        report: WorkerReport,
        error: str,
        *,
        integration_status: str | None = None,
    ) -> None:
        persisted = self._runtime._store.update_delegation_task(
            f"{run_id}:{task.id}",
            status=report.status.value,
            child_session_id=(
                report.session_id
                if not report.session_id.startswith(
                    ("failed:", "blocked:", "cancelled:")
                )
                else None
            ),
            generation=report.generation,
            report=report.to_dict(),
            error=error,
            integration_status=integration_status,
            expected_statuses=("queued", "running"),
        )
        if not persisted:
            return
        self._emit(
            (
                "delegation_task_failed"
                if report.status
                in {WorkerReportStatus.FAILED, WorkerReportStatus.CANCELLED}
                else "delegation_task_reported"
            ),
            run_id,
            {
                "task_id": task.id,
                "child_session_id": report.session_id,
                "agent_type": task.agent_type,
                "status": report.status.value,
                "tokens_used": report.tokens_used,
                "duration_ms": report.duration_ms,
            },
        )

    @staticmethod
    def _worker_report(
        task: _BatchTask,
        result: AgentRunResult,
        *,
        generation: int,
        duration_ms: int,
    ) -> WorkerReport:
        mapping = {
            AgentRunStatus.COMPLETED: WorkerReportStatus.COMPLETED,
            AgentRunStatus.PARTIAL: WorkerReportStatus.PARTIAL,
            AgentRunStatus.FAILED: WorkerReportStatus.FAILED,
            AgentRunStatus.CANCELLED: WorkerReportStatus.CANCELLED,
        }
        evidence = result.worktree
        changed_files = tuple(
            ChangedFile(path=path)
            for path in (evidence.changed_files if evidence is not None else ())
        )
        warnings = tuple(
            item for item in (result.warning, result.failure_diagnosis) if item
        )
        return WorkerReport(
            task_id=task.id,
            session_id=result.session_id,
            generation=generation,
            agent_type=task.agent_type,
            status=mapping[result.status],
            summary=result.summary,
            findings=result.structured_findings,
            changed_files=changed_files,
            unresolved=(
                (result.error or result.failure_diagnosis,)
                if result.status in {
                    AgentRunStatus.PARTIAL,
                    AgentRunStatus.FAILED,
                    AgentRunStatus.CANCELLED,
                }
                and (result.error or result.failure_diagnosis)
                else ()
            ),
            warnings=warnings,
            tokens_used=result.tokens_used,
            budget_settled=result.budget_settled,
            duration_ms=duration_ms,
            worktree=evidence.to_dict() if evidence is not None else None,
        )

    def _parse_tasks(self, raw: Any) -> tuple[_BatchTask, ...]:
        max_tasks = self._feature_config().max_tasks
        if not isinstance(raw, list) or not 2 <= len(raw) <= max_tasks:
            raise ValueError(
                "AgentBatch requires at least 2 tasks and at most "
                f"GRACE_MAX_MULTI_AGENT_TASKS ({max_tasks})"
            )
        tasks = []
        for item in raw:
            if not isinstance(item, dict):
                raise TypeError("Each AgentBatch task must be an object")
            task_id = str(item.get("id", "")).strip()
            agent_type = str(item.get("agent", "")).strip()
            goal = str(item.get("goal", "")).strip()
            prompt = str(item.get("prompt", "")).strip()
            purpose = str(item.get("purpose", "general"))
            if not all((task_id, goal, prompt)):
                raise ValueError("Each task requires id, goal, and prompt")
            if not agent_type:
                agent_type = self._route_agent(
                    task_id=task_id,
                    goal=goal,
                    purpose=purpose,
                    expected_files=tuple(
                        str(value) for value in item.get("expected_files", [])
                    ),
                    write_files=tuple(
                        str(value) for value in item.get("write_files", [])
                    ),
                )
            isolation_raw = item.get("isolation")
            tasks.append(_BatchTask(
                id=task_id,
                agent_type=agent_type,
                goal=goal,
                prompt=prompt,
                purpose=purpose,
                scope=tuple(str(value) for value in item.get("scope", [])),
                dependencies=tuple(
                    str(value) for value in item.get("depends_on", [])
                ),
                expected_files=tuple(
                    str(value) for value in item.get("expected_files", [])
                ),
                write_files=tuple(
                    str(value) for value in item.get("write_files", [])
                ),
                required=bool(item.get("required", True)),
                model=(
                    str(item["model"]) if item.get("model") is not None else None
                ),
                isolation=(
                    WorkspaceMode(isolation_raw)
                    if isolation_raw is not None else None
                ),
            ))
        return tuple(tasks)

    def _route_agent(
        self,
        *,
        task_id: str,
        goal: str,
        purpose: str,
        expected_files: tuple[str, ...],
        write_files: tuple[str, ...],
    ) -> str:
        from agent.session.subagent_router import RouterPolicy, SubagentRouter
        from agent.session.task_shape import TaskPurpose, WorkItem

        try:
            typed_purpose = TaskPurpose(purpose)
        except ValueError:
            typed_purpose = TaskPurpose.GENERAL
        parent = self._runtime.agent_registry.get(self._caller_agent_name)
        allowed = frozenset(
            child.name
            for child in self._runtime.agent_registry.delegatable_by(parent)
        )
        available = frozenset(
            child.name for child in self._runtime.agent_registry.list_all()
        )
        route = SubagentRouter().route(
            WorkItem(
                id=task_id,
                goal=goal,
                domain=typed_purpose.value,
                expected_files=expected_files,
                write_files=write_files,
            ),
            typed_purpose,
            RouterPolicy(
                parent_intent=parent.intent,
                allowed_agents=allowed,
                available_agents=available,
            ),
        )
        return route.agent_name

    def _validate(self, tasks: tuple[_BatchTask, ...], topology: str) -> None:
        if topology not in {"fan_out_fan_in", "chain"}:
            raise ValueError("AgentBatch topology must be fan_out_fan_in or chain")
        config = self._feature_config()
        if len(tasks) > config.max_tasks:
            raise ValueError(
                "AgentBatch task count exceeds GRACE_MAX_MULTI_AGENT_TASKS "
                f"({config.max_tasks})"
            )
        ids = {task.id for task in tasks}
        if len(ids) != len(tasks):
            raise ValueError("AgentBatch task ids must be unique")
        allowed = {
            item.name
            for item in self._runtime.agent_registry.delegatable_by(
                self._runtime.agent_registry.get(self._caller_agent_name)
            )
        }
        for task in tasks:
            if task.agent_type not in allowed:
                raise ValueError(
                    f"Agent {task.agent_type!r} is not delegatable; "
                    f"available: {sorted(allowed)}"
                )
            unknown = set(task.dependencies) - ids
            if unknown:
                raise ValueError(
                    f"Task {task.id!r} has unknown dependencies: {sorted(unknown)}"
                )
            if task.id in task.dependencies:
                raise ValueError(f"Task {task.id!r} cannot depend on itself")
        active_write_sets: list[tuple[str, set[str]]] = []
        for task in tasks:
            definition = self._runtime.agent_registry.get(task.agent_type)
            workspace = task.isolation or definition.workspace_mode
            if definition.intent is TaskIntent.EDIT and not task.write_files:
                raise ValueError(
                    f"Write task {task.id!r} must declare non-empty write_files"
                )
            if task.write_files and workspace is WorkspaceMode.CURRENT:
                write_set = set(task.write_files)
                for other_id, other_set in active_write_sets:
                    overlap = write_set & other_set
                    if overlap:
                        raise ValueError(
                            "Parallel shared-workspace write conflict between "
                            f"{other_id!r} and {task.id!r}: {sorted(overlap)}"
                        )
                active_write_sets.append((task.id, write_set))

    def _plan_topology(
        self, tasks: tuple[_BatchTask, ...], requested_topology: str,
    ):
        from agent.session.task_shape import (
            AgentTopology,
            ContextVolume,
            CoordinationNeed,
            EvidenceLevel,
            RiskLevel,
            TaskPurpose,
            TaskShape,
            WorkItem,
        )
        from agent.session.topology_planner import TopologyPlanner, TopologyPolicy

        parent = self._runtime._store.get_session(self._parent_session_id)
        if parent is None:
            raise ValueError(f"Unknown parent session: {self._parent_session_id}")
        all_records = []
        pending = [self._runtime._store.get_session(parent.root_id or parent.id)]
        while pending:
            record = pending.pop()
            if record is None:
                continue
            all_records.append(record)
            pending.extend(self._runtime._store.list_child_sessions(record.id))
        terminal = {"completed", "partial", "failed", "cancelled"}
        spawned = sum(record.parent_id is not None for record in all_records)
        active = sum(
            record.parent_id is not None
            and getattr(record.status, "value", record.status) not in terminal
            for record in all_records
        )
        parent_definition = self._runtime.agent_registry.get(
            self._caller_agent_name
        )
        purpose_values = []
        for task in tasks:
            try:
                purpose_values.append(TaskPurpose(task.purpose))
            except ValueError:
                purpose_values.append(TaskPurpose.GENERAL)
        overall_purpose = (
            purpose_values[0]
            if len(set(purpose_values)) == 1 else TaskPurpose.GENERAL
        )
        shape = TaskShape(
            intent=parent_definition.intent,
            purpose=overall_purpose,
            domains=tuple(dict.fromkeys(item.value for item in purpose_values)),
            work_items=tuple(
                WorkItem(
                    id=task.id,
                    goal=task.goal,
                    domain=purpose.value,
                    candidate_agent=task.agent_type,
                    depends_on=task.dependencies,
                    expected_files=task.expected_files,
                    write_files=task.write_files,
                    deliverable="structured WorkerReport",
                    required=task.required,
                    estimated_tokens=2_000,
                )
                for task, purpose in zip(tasks, purpose_values)
            ),
            expected_files=tuple(dict.fromkeys(
                path for task in tasks for path in task.expected_files
            )),
            write_files=tuple(dict.fromkeys(
                path for task in tasks for path in task.write_files
            )),
            context_volume=(
                ContextVolume.LARGE if len(tasks) >= 3 else ContextVolume.MEDIUM
            ),
            evidence_requirement=EvidenceLevel.FILE_LINE,
            coordination_need=CoordinationNeed.PARENT_MEDIATED,
            risk=RiskLevel.MEDIUM,
            user_requested_topology=AgentTopology(requested_topology),
        )
        safety_limits = SubagentSafetyLimits.from_environment()
        return TopologyPlanner().plan(
            shape,
            TopologyPolicy(
                # Topology validation uses the independent total DAG bound.
                # Execution below still slices each ready set into bounded waves.
                max_fanout=self._feature_config().max_tasks,
                max_concurrent_subagents=self._max_concurrent(runtime=self._runtime),
                max_spawn_per_session=safety_limits.max_spawn_per_session,
                max_subagent_spawn_depth=safety_limits.max_spawn_depth,
                current_depth=int(parent.agent_depth.value),
                spawned_count=spawned,
                active_count=active,
                available_tokens=self._run_context.budget.token_remaining,
                minimum_worker_tokens=2_000,
                nested_enabled=safety_limits.max_spawn_depth > 1,
                worktree_writes=all(
                    not task.write_files
                    or (
                        task.isolation
                        or self._runtime.agent_registry.get(
                            task.agent_type
                        ).workspace_mode
                    ) is WorkspaceMode.WORKTREE
                    for task in tasks
                ),
            ),
        )

    def _is_parallel_safe(self, task: _BatchTask) -> bool:
        definition = self._runtime.agent_registry.get(task.agent_type)
        workspace = task.isolation or definition.workspace_mode
        return (
            definition.intent is TaskIntent.ANALYSIS
            or workspace is WorkspaceMode.WORKTREE
        )

    @staticmethod
    def _infer_topology(tasks: tuple[_BatchTask, ...]) -> str:
        return (
            "chain"
            if any(task.dependencies for task in tasks)
            else "fan_out_fan_in"
        )

    @staticmethod
    def _feature_config():
        from agent.session.multi_agent_config import MultiAgentFeatureConfig

        return MultiAgentFeatureConfig.from_environment()

    @staticmethod
    def _max_concurrent(runtime=None) -> int:
        """Return effective max concurrent workers.

        When a ResourceGovernor is available and not in observe mode,
        uses the governor's available worker slots. Otherwise falls
        back to the env-var-based MultiAgentFeatureConfig.
        """
        if runtime is not None:
            governor = getattr(runtime, "_governor", None)
            if governor is not None and governor.mode != "observe":
                from core.resource_governor import ResourceKind
                snap = governor.snapshot()
                ws = snap.snapshots.get(ResourceKind.WORKER_SLOT)
                if ws is not None and ws.limit > 0:
                    return min(ws.available, ws.limit)
        return AgentBatchTool._feature_config().max_concurrent

    @staticmethod
    def _format_result(
        run_id: str,
        topology: str,
        reports: list[WorkerReport],
    ) -> str:
        lines = [
            f"<delegation-result run_id='{run_id}' topology='{topology}'>",
        ]
        for report in reports:
            lines.append(
                f"<worker task_id='{report.task_id}' "
                f"agent='{report.agent_type}' status='{report.status.value}'>"
            )
            lines.append(report.summary)
            if report.unresolved:
                lines.append(
                    "Unresolved: " + "; ".join(report.unresolved)
                )
            lines.append("</worker>")
        lines.append(
            "Synthesize these reports, verify conflicting claims, and do not "
            "forward them verbatim."
        )
        lines.append("</delegation-result>")
        return "\n".join(lines)

    def _emit(
        self, event_type: str, run_id: str, payload: dict[str, Any],
    ) -> None:
        callback = getattr(self._runtime, "_event_callback", None)
        if callback is None:
            return
        from agent.task import Event, EventType

        try:
            callback(Event(
                event_type=EventType(event_type),
                task_id=run_id,
                session_id=self._parent_session_id,
                payload={
                    "delegation_run_id": run_id,
                    "parent_session_id": self._parent_session_id,
                    **payload,
                },
            ))
        except Exception:
            # Observability cannot change execution semantics.
            return
