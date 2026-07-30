"""agent/session/runtime_spawn.py

SessionRuntime 的子代理生成逻辑。
函数被绑定到 SessionRuntime 类上作为方法。
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from agent.session.models import (
    AgentKind,
    AgentSpawnRequest,
    ContextOrigin,
    DelegationOrigin,
    ExecutionPlacement,
    ForkStatus,
    AgentRunStatus,
    
    SessionMode,
    SessionStatus,
)
from agent.session.run_context import AgentSpawnContext, CancellationToken, ToolSchemaSnapshot
from agent.session.task_contract import TaskContract
from hooks.events import HookContext, HookEvent
from llm.base import LLMMessage
from config.env import SubagentSafetyLimits

if TYPE_CHECKING:
    from agent.session.runtime import SessionRuntime
    from core.policy import PhasePolicy

logger = logging.getLogger(__name__)


def _descendants(store, root_id: str):
    root = store.get_session(root_id)
    if root is None:
        return []
    records = []
    pending = [root]
    seen = set()
    while pending:
        record = pending.pop()
        if record.id in seen:
            continue
        seen.add(record.id)
        records.append(record)
        pending.extend(store.list_child_sessions(record.id))
    return records


def spawn_agent(
    self: "SessionRuntime", *, parent_session_id: str,
    request: AgentSpawnRequest, budget_tokens: int, parent_max_steps: int,
    cancellation_token: CancellationToken, parent_policy: "PhasePolicy",
    origin: DelegationOrigin = DelegationOrigin.TOOL,
    spawn_context: AgentSpawnContext | None = None,
    execution_repo_path: str | None = None,
    child_metadata: dict[str, object] | None = None,
    child_created_callback=None,
    parent_budget=None,
):
    from core.policy import PhasePolicy
    if budget_tokens <= 0:
        raise ValueError("child budget_tokens must be positive")
    if parent_max_steps <= 0:
        raise ValueError("child parent_max_steps must be positive")
    if not isinstance(request, AgentSpawnRequest):
        raise TypeError("request must be an AgentSpawnRequest")
    if not isinstance(cancellation_token, CancellationToken):
        raise TypeError("child cancellation_token must be a CancellationToken")
    if not isinstance(parent_policy, PhasePolicy):
        raise TypeError("child parent_policy must be a PhasePolicy")
    if not isinstance(origin, DelegationOrigin):
        origin = DelegationOrigin(origin)
    parent = self._store.get_session(parent_session_id)
    if parent is None:
        raise ValueError(f"Unknown session: {parent_session_id}")
    safety_limits = SubagentSafetyLimits.from_environment()
    max_depth = min(
        safety_limits.max_spawn_depth,
        parent.agent_depth.MAX_SUBAGENT_DEPTH,
    )
    if parent.agent_depth.value >= max_depth:
        raise ValueError(
            f"Maximum configured subagent depth reached ({max_depth})"
        )
    parent_definition = self._agent_registry.get(parent.agent_name)
    if request.agent_kind is AgentKind.NAMED_SUBAGENT:
        definition = request.definition
        if definition is None:
            raise ValueError("Named spawn requires a definition")
        allowed_names = {c.name for c in self._agent_registry.delegatable_by(parent_definition)}
        if definition.name not in allowed_names:
            raise ValueError(f"Agent {definition.name!r} not delegatable by {parent.agent_name!r}")
    else:
        if parent.agent_kind is AgentKind.FORK:
            raise ValueError("A fork cannot spawn another fork")
        if spawn_context is None:
            raise ValueError("Fork spawn requires a live parent snapshot")
        definition = parent_definition
    is_fork = request.agent_kind is AgentKind.FORK
    child_agent_type = (
        AgentKind.FORK.value
        if is_fork
        else definition.name
    )
    child_contract = TaskContract.for_subagent(
        definition, self._root_agent_config,
        parent_budget_tokens=budget_tokens, parent_max_steps=parent_max_steps,
    )
    parent_repo = self._require_project_scope(parent.repo_path)
    _repo = (
        self._require_review_snapshot_scope(parent_repo, execution_repo_path)
        if execution_repo_path is not None
        else parent_repo
    )
    if spawn_context is not None:
        if not isinstance(spawn_context, AgentSpawnContext):
            raise TypeError("spawn_context must be an AgentSpawnContext")
        if spawn_context.parent_session_id != parent.id:
            raise ValueError("spawn context parent does not match the session")
        if spawn_context.parent_agent_name != parent.agent_name:
            raise ValueError("spawn context agent does not match the session")
        if self._require_project_scope(spawn_context.repo_path) != _repo:
            raise ValueError("spawn context repo does not match the session")
        if (
            is_fork
            and spawn_context.model_name != self._backend.model_name
        ):
            raise ValueError("Fork model must match the parent model")
    # Preserve the lifetime spawn guard independently from the renewable
    # concurrency governor.  The two limits have different semantics.
    with self._spawn_lock:
        records = _descendants(self._store, parent.root_id or parent.id)
        max_spawn = safety_limits.max_spawn_per_session
        spawned = sum(record.parent_id is not None for record in records)
        if spawned + self._spawn_reservations >= max_spawn:
            raise ValueError(
                f"Subagent spawn limit reached ({max_spawn}); complete directly"
            )
        self._spawn_reservations += 1

    # ResourceGovernor owns renewable execution capacity only.  The child's
    # token ceiling is already derived once above by TaskContract from the
    # parent ExecutionBudget.  Reserving an estimated TOKEN_BUDGET here created
    # a second, weaker budget authority whose estimate could disagree with the
    # executable contract.
    governor = getattr(self, "_governor", None)
    gov_lease = None
    budget_reservation = None
    budget_event_request = None
    try:
        if parent_budget is not None:
            budget_reservation = parent_budget.reserve_tokens(
                child_contract.budget_tokens,
            )
        if governor is not None:
            from core.resource_governor import (
                AdmissionOutcome,
                ResourceAdmissionError,
                ResourceKind,
                ResourceRequest,
            )
            root_id = parent.root_id or parent.id
            governance_cfg = getattr(governor, "_config", None)
            queue_cfg = getattr(governance_cfg, "queue", None)
            queue_timeout = float(
                getattr(queue_cfg, "timeout_seconds", 120.0)
            )
            metadata = child_metadata or {}
            gov_request = ResourceRequest(
                request_id=f"spawn-{uuid.uuid4().hex}",
                root_session_id=root_id,
                session_id=parent.id,
                run_id=str(metadata.get("delegation_run_id", "")),
                task_id=str(metadata.get("delegation_task_id", "")),
                resources={ResourceKind.WORKER_SLOT: 1},
                timeout_s=queue_timeout,
                cancel_token=cancellation_token,
            )
            gov_result = governor.admit_wait(gov_request)
            if gov_result.outcome != AdmissionOutcome.GRANTED:
                raise ResourceAdmissionError(
                    gov_result.outcome,
                    ResourceKind.WORKER_SLOT,
                    gov_result.reason,
                )
            gov_lease = gov_result.lease
            if budget_reservation is not None:
                budget_event_request = ResourceRequest(
                    request_id=f"{gov_request.request_id}:budget",
                    root_session_id=gov_request.root_session_id,
                    session_id=gov_request.session_id,
                    run_id=gov_request.run_id,
                    task_id=gov_request.task_id,
                    resources={},
                )
                governor.publish_accounting_event(
                    "granted",
                    budget_event_request,
                    {
                        ResourceKind.TOKEN_BUDGET:
                            budget_reservation.reserved_tokens,
                    },
                )
        else:
            # Legacy concurrency check when no governor is installed.
            from agent.session.multi_agent_config import MultiAgentFeatureConfig

            max_concurrent = (
                MultiAgentFeatureConfig.from_environment().max_concurrent
            )
            terminal = {
                SessionStatus.COMPLETED,
                SessionStatus.PARTIAL,
                SessionStatus.FAILED,
                SessionStatus.CANCELLED,
            }
            active = sum(
                record.parent_id is not None and record.status not in terminal
                for record in records
            )
            if active + self._spawn_reservations > max_concurrent:
                raise ValueError(
                    f"Concurrent subagent limit reached ({max_concurrent}); do not "
                    "retry until a running child completes"
                )
    except Exception:
        if gov_lease is not None:
            gov_lease.release()
        if budget_reservation is not None:
            budget_reservation.release()
        with self._spawn_lock:
            self._spawn_reservations = max(0, self._spawn_reservations - 1)
        raise
    try:
        child = self._store.create_session(
            agent_name=definition.name, mode=SessionMode.SUBAGENT,
            agent_kind=request.agent_kind, context_origin=request.context_origin,
            execution_placement=request.execution_placement,
            workspace_mode=request.workspace_mode, repo_path=_repo,
            title=request.description[:80] or definition.name,
            parent_id=parent.id, root_id=parent.root_id,
            metadata={
            **(child_metadata or {}),
            "entrypoint": origin.value,
            "agent_kind": request.agent_kind.value,
            "context_origin": request.context_origin.value,
            "workspace_mode": request.workspace_mode.value,
            "intent": definition.intent.value,
            "requested_budget_tokens": budget_tokens,
            "budget_tokens": child_contract.budget_tokens,
            "max_steps": child_contract.max_steps,
            "parent_policy": parent_policy.to_dict(),
            "parent_snapshot_fingerprint": (
                spawn_context.conversation.fingerprint
                if spawn_context is not None else None
            ),
            "parent_snapshot_message_count": (
                len(spawn_context.conversation.messages)
                if spawn_context is not None else 0
            ),
            "model_name": (
                spawn_context.model_name
                if spawn_context is not None else self._backend.model_name
            ),
            "parent_tool_schemas": (
                [
                    {
                        "name": schema.name,
                        "description": schema.description,
                        "parameters_json": schema.parameters_json,
                        "prompt_contract": list(schema.prompt_contract),
                    }
                    for schema in spawn_context.tool_schemas
                ]
                if is_fork and spawn_context is not None
                else []
            ),
            "source_repo_path": parent_repo,
            },
        )
    except Exception:
        # Release governor lease on session creation failure (lease is
        # idempotent — _execute_child_session's finally is a safety net).
        if budget_event_request is not None:
            from core.resource_governor import ResourceKind

            governor.publish_accounting_event(
                "reconciled",
                budget_event_request,
                {
                    ResourceKind.TOKEN_BUDGET:
                        budget_reservation.reserved_tokens,
                },
                actual={ResourceKind.TOKEN_BUDGET: 0},
            )
        if gov_lease is not None:
            gov_lease.release()
        if budget_reservation is not None:
            budget_reservation.release()
        raise
    finally:
        with self._spawn_lock:
            self._spawn_reservations = max(0, self._spawn_reservations - 1)
    if child_created_callback is not None:
        try:
            child_created_callback(child)
        except Exception:
            logger.exception(
                "Child creation callback failed for session %s",
                child.id,
            )
    child_cancellation = cancellation_token.child()
    self._cancellation_tokens[(child.id, child.generation)] = child_cancellation
    if request.agent_kind is AgentKind.FORK:
        for msg in spawn_context.conversation.materialize():
            self._store.append_message(child.id, msg)
    self._store.append_message(child.id, LLMMessage(role="user", content=request.prompt))
    self._store.update_status(child.id, SessionStatus.RUNNING)
    from agent.task import EventType
    self._emit_subagent_event(
        EventType.SUBAGENT_START, parent_session_id=parent.id,
        root_session_id=parent.root_id, child_session_id=child.id,
        agent_name=child_agent_type, status=SessionStatus.RUNNING,
    )
    self._fire_hook(HookContext(
        event=HookEvent.SUBAGENT_START, session_id=parent.id,
        agent_id=child.id, agent_type=child_agent_type,
    ))

    # Subagent permission inheritance (CC-aligned: parent mode overrides child)
    _child_permission_mode = self._resolve_child_permission_mode(
        parent_definition,
        definition if request.agent_kind is AgentKind.NAMED_SUBAGENT else None,
    )
    if _child_permission_mode:
        child.metadata["permission_mode_override"] = _child_permission_mode

    # Connect agent-scoped MCP servers (CC-aligned: inline mcpServers)
    _agent_mcp_tools = []
    if self._mcp_integration is not None and not is_fork:
        _agent_mcp_tools = self._mcp_integration.connect_agent_servers(definition)

    execute = lambda: self._execute_child_session(
        parent=parent, child=child, request=request,
        definition=definition, parent_definition=parent_definition,
        contract=child_contract, cancellation_token=child_cancellation,
        parent_policy=parent_policy, repo_path=_repo,
        child_agent_type=child_agent_type, spawn_context=spawn_context,
        gov_lease=gov_lease,
        budget_reservation=budget_reservation,
        budget_event_request=budget_event_request,
    )
    _need_mcp_cleanup = bool(_agent_mcp_tools) and self._mcp_integration is not None
    cleanup = None
    if _need_mcp_cleanup:
        cleanup = lambda: self._mcp_integration.disconnect_agent_servers(definition)

    if request.execution_placement is ExecutionPlacement.FOREGROUND:
        try:
            return execute()
        finally:
            if cleanup is not None:
                cleanup()
    return self._start_background_execution(
        parent=parent, child=child, agent_name=definition.name,
        execute=execute, cleanup=cleanup,
    )


def _execute_child_session(self: "SessionRuntime", *, parent, child, request,
                           definition, parent_definition, contract, cancellation_token,
                           parent_policy, repo_path, child_agent_type, spawn_context,
                           persisted_messages=None, gov_lease=None,
                           budget_reservation=None, budget_event_request=None):
    child_result = None
    child_error = ""
    def _persist(msgs):
        for m in msgs:
            self._store.append_message(child.id, m)
    try:
        inherited_registry = None
        if request.agent_kind is AgentKind.FORK:
            inherited_registry = self._build_registry_for_session(
                parent_definition, child,
            ).with_phase_policy(parent_policy)
            if request.context_origin is ContextOrigin.PARENT_SNAPSHOT:
                if spawn_context is None:
                    raise ValueError("Fork spawn requires a live parent snapshot")
                live_schemas = tuple(
                    ToolSchemaSnapshot.capture(schema)
                    for schema in inherited_registry.get_schemas()
                )
                if live_schemas != spawn_context.tool_schemas:
                    raise ValueError(
                        "Fork tool contract changed after the parent model call"
                    )
            else:
                raw_schemas = child.metadata.get("parent_tool_schemas")
                if not isinstance(raw_schemas, list) or not raw_schemas:
                    raise ValueError(
                        "Fork resume requires its persisted tool contract"
                    )
                expected_schemas = tuple(
                    ToolSchemaSnapshot(
                        name=str(item["name"]),
                        description=str(item["description"]),
                        parameters_json=str(item["parameters_json"]),
                        prompt_contract=tuple(item.get("prompt_contract", [])),
                    )
                    for item in raw_schemas
                    if isinstance(item, dict)
                )
                live_schemas = tuple(
                    ToolSchemaSnapshot.capture(schema)
                    for schema in inherited_registry.get_schemas()
                )
                if live_schemas != expected_schemas:
                    raise ValueError(
                        "Fork tool contract changed since its prior generation"
                    )
        # ── Snapshot parent pipeline state for child inheritance ──
        _inherited_state = self._base_registry.permission_inheritable_state()
        from agent.session.subagent import run_child_agent
        child_result = run_child_agent(
            agent_id=child.id, request=request, source_definition=definition,
            repo_path=repo_path, base_registry=self._base_registry,
            backend=self.get_backend_for_session(parent.id),
            log_dir=self._log_dir,
            root_agent_config=self._root_agent_config, message_sink=_persist,
            contract=contract, cancellation_token=cancellation_token,
            parent_policy=parent_policy, spawn_context=spawn_context,
            inherited_registry=inherited_registry,
            event_callback=self._event_callback,
            persisted_messages=persisted_messages,
            session_record=child, session_runtime=self,
            parent_pipeline_state=_inherited_state,
        )
        if budget_reservation is not None:
            from dataclasses import replace

            budget_reservation.settle(child_result.tokens_used)
            child_result = replace(child_result, budget_settled=True)
        self._store.set_agent_result(child.id, child_result)
        if child_result.summary:
            self._store.append_message(
                child.id,
                LLMMessage(role="assistant", content=child_result.summary),
            )
        return child_result
    except Exception as exc:
        child_error = str(exc) or type(exc).__name__
        raise
    finally:
        if child_result is not None and child_result.status is ForkStatus.CANCELLED:
            self._store.update_status(
                child.id, SessionStatus.CANCELLED,
                error=child_result.error or child_result.summary,
            )
            if child_result.summary:
                self._store.set_summary(
                    child.id,
                    child_result.summary,
                    status=SessionStatus.CANCELLED,
                )
        elif child_result is None or child_result.status is ForkStatus.FAILED:
            summary = child_result.summary if child_result is not None else ""
            err = (
                (child_result.error or summary)
                if child_result is not None
                else (child_error or "Subagent execution failed")
            )
            self._store.update_status(child.id, SessionStatus.FAILED, error=err)
            if summary:
                self._store.set_summary(
                    child.id, summary, status=SessionStatus.FAILED,
                )
        elif child_result.status is ForkStatus.PARTIAL:
            self._store.set_summary(child.id, child_result.summary, status=SessionStatus.PARTIAL)
        else:
            self._store.set_summary(child.id, child_result.summary, status=SessionStatus.COMPLETED)
        completed = self._store.get_session(child.id)
        if completed is not None:
            from agent.task import EventType
            self._emit_subagent_event(
                EventType.SUBAGENT_STOP, parent_session_id=parent.id,
                root_session_id=parent.root_id, child_session_id=child.id,
                agent_name=child_agent_type, status=completed.status,
                fork_result=child_result,
            )
            delegation_task_id = str(
                (completed.metadata or {}).get("delegation_task_id", "")
            )
            delegation_run_id = str(
                (completed.metadata or {}).get("delegation_run_id", "")
            )
            if delegation_task_id and delegation_run_id:
                try:
                    from agent.session.result_contract import (
                        ChangedFile,
                        WorkerReport,
                        WorkerReportStatus,
                    )

                    status_map = {
                        AgentRunStatus.COMPLETED: WorkerReportStatus.COMPLETED,
                        AgentRunStatus.PARTIAL: WorkerReportStatus.PARTIAL,
                        AgentRunStatus.FAILED: WorkerReportStatus.FAILED,
                        AgentRunStatus.CANCELLED: WorkerReportStatus.CANCELLED,
                    }
                    effective_status = (
                        child_result.status
                        if child_result is not None
                        else AgentRunStatus.FAILED
                    )
                    evidence = (
                        child_result.worktree
                        if child_result is not None else None
                    )
                    report = WorkerReport(
                        task_id=delegation_task_id.rsplit(":", 1)[-1],
                        session_id=completed.id,
                        generation=int(completed.generation),
                        agent_type=child_agent_type,
                        status=status_map[effective_status],
                        summary=(
                            child_result.summary
                            if child_result is not None else child_error
                        ),
                        findings=(
                            child_result.structured_findings
                            if child_result is not None else ()
                        ),
                        changed_files=tuple(
                            ChangedFile(path=path)
                            for path in (
                                evidence.changed_files
                                if evidence is not None else ()
                            )
                        ),
                        unresolved=(
                            (child_error,)
                            if child_result is None and child_error else ()
                        ),
                        warnings=tuple(
                            item
                            for item in (
                                (
                                    child_result.warning
                                    if child_result is not None else ""
                                ),
                                (
                                    child_result.failure_diagnosis
                                    if child_result is not None else ""
                                ),
                            )
                            if item
                        ),
                        tokens_used=(
                            child_result.tokens_used
                            if child_result is not None else 0
                        ),
                        budget_settled=(
                            child_result.budget_settled
                            if child_result is not None else False
                        ),
                        worktree=(
                            evidence.to_dict()
                            if evidence is not None else None
                        ),
                    )
                    self._store.update_delegation_task(
                        delegation_task_id,
                        status=report.status.value,
                        child_session_id=completed.id,
                        generation=int(completed.generation),
                        report=report.to_dict(),
                        error=(
                            child_result.error
                            if child_result is not None else child_error
                        ),
                    )
                    tasks = self._store.list_delegation_tasks(
                        delegation_run_id
                    )
                    # A multi-task AgentBatch owns run-level synthesis and
                    # terminal reconciliation. Child completion only persists
                    # task facts; finalizing here races the batch owner and can
                    # bypass its exactly-once terminal broadcast. A one-to-one
                    # Agent delegation has no separate coordinator, so finalize
                    # and publish its persisted terminal here.
                    if len(tasks) == 1:
                        converged = self._store.reconcile_delegation_run(
                            delegation_run_id
                        )
                        terminal_event = converged.pop("_terminal_event", None)
                        callback = getattr(self, "_event_callback", None)
                        if isinstance(terminal_event, dict) and callback is not None:
                            from agent.task import Event, EventType

                            callback(Event(
                                event_type=EventType.DELEGATION_COMPLETED,
                                task_id=delegation_run_id,
                                session_id=str(converged["parent_session_id"]),
                                payload={
                                    "delegation_run_id": delegation_run_id,
                                    "parent_session_id": str(
                                        converged["parent_session_id"]
                                    ),
                                    "_persisted_event": terminal_event,
                                },
                            ))
                except Exception:
                    logger.exception(
                        "Failed to persist delegation result for child %s — "
                        "delegation task %s in run %s may be stuck",
                        completed.id,
                        delegation_task_id,
                        delegation_run_id,
                    )
                    # Best-effort: mark the task as failed so the coordinator
                    # does not wait indefinitely.
                    try:
                        self._store.update_delegation_task(
                            delegation_task_id,
                            status="failed",
                            error=(
                                "Child completed but delegation persistence "
                                "failed — manual retry required"
                            ),
                            expected_statuses=("queued", "running"),
                        )
                    except Exception:
                        logger.exception(
                            "Could not mark stuck delegation task %s as failed",
                            delegation_task_id,
                        )
                    # Single-task runs have no separate coordinator to detect
                    # the stuck task — finalize the run as partial so the
                    # control plane surfaces it.
                    if len(tasks) == 1:
                        try:
                            current = self._store.get_delegation_run(
                                delegation_run_id,
                            ) or {}
                            self._store.finalize_delegation_run(
                                delegation_run_id,
                                status="partial",
                                phase="recovery_required",
                                expected_version=int(
                                    current.get("version", 0),
                                ),
                            )
                        except Exception:
                            logger.exception(
                                "Could not finalize stuck delegation run %s",
                                delegation_run_id,
                            )
        self._cancellation_tokens.pop(
            (child.id, child.generation), None,
        )
        # Token usage is settled by the parent ExecutionBudget through the
        # delegation ToolResult.  This lease only returns renewable capacity.
        if budget_event_request is not None:
            from core.resource_governor import ResourceKind

            used_tokens = (
                child_result.tokens_used
                if child_result is not None else 0
            )
            self._governor.publish_accounting_event(
                "reconciled",
                budget_event_request,
                {
                    ResourceKind.TOKEN_BUDGET:
                        budget_reservation.reserved_tokens,
                },
                actual={ResourceKind.TOKEN_BUDGET: used_tokens},
            )
        if gov_lease is not None and not gov_lease.is_released():
            gov_lease.release()
        if budget_reservation is not None:
            budget_reservation.release()
