"""agent/session/runtime_spawn.py

SessionRuntime 的子代理生成逻辑。
函数被绑定到 SessionRuntime 类上作为方法。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent.session.models import (
    AgentKind,
    AgentSpawnRequest,
    ContextOrigin,
    DelegationOrigin,
    ExecutionPlacement,
    ForkStatus,
    
    SessionMode,
    SessionStatus,
)
from agent.session.run_context import AgentSpawnContext, CancellationToken, ToolSchemaSnapshot
from agent.session.task_contract import TaskContract
from hooks.events import HookContext, HookEvent
from llm.base import LLMMessage

if TYPE_CHECKING:
    from agent.session.runtime import SessionRuntime

logger = logging.getLogger(__name__)


def spawn_agent(
    self: "SessionRuntime", *, parent_session_id: str,
    request: AgentSpawnRequest, budget_tokens: int, parent_max_steps: int,
    cancellation_token: CancellationToken, parent_policy: "PhasePolicy",
    origin: DelegationOrigin = DelegationOrigin.TOOL,
    spawn_context: AgentSpawnContext | None = None,
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
    if not parent.agent_depth.can_spawn:
        raise ValueError("Maximum subagent depth reached")
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
    _repo = self._require_project_scope(parent.repo_path)
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
    child = self._store.create_session(
        agent_name=definition.name, mode=SessionMode.SUBAGENT,
        agent_kind=request.agent_kind, context_origin=request.context_origin,
        execution_placement=request.execution_placement,
        workspace_mode=request.workspace_mode, repo_path=_repo,
        title=request.description[:80] or definition.name,
        parent_id=parent.id, root_id=parent.root_id,
        metadata={
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
                    }
                    for schema in spawn_context.tool_schemas
                ]
                if is_fork and spawn_context is not None
                else []
            ),
        },
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
                           persisted_messages=None):
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
        self._store.set_agent_result(child.id, child_result)
        self._store.append_message(child.id, LLMMessage(role="assistant", content=child_result.summary))
        return child_result
    except Exception as exc:
        child_error = str(exc) or type(exc).__name__
        self._store.append_message(
            child.id, LLMMessage(role="assistant", content=f"Subagent failed: {exc}"),
        )
        raise
    finally:
        if child_result is not None and child_result.status is ForkStatus.CANCELLED:
            self._store.update_status(
                child.id, SessionStatus.CANCELLED,
                error=child_result.error or child_result.summary,
            )
            self._store.set_summary(child.id, child_result.summary, status=SessionStatus.CANCELLED)
        elif child_result is None or child_result.status is ForkStatus.FAILED:
            summary = child_result.summary if child_result is not None else "Subagent execution failed"
            err = (child_result.error or summary) if child_result is not None else (child_error or summary)
            self._store.update_status(child.id, SessionStatus.FAILED, error=err)
            self._store.set_summary(child.id, summary, status=SessionStatus.FAILED)
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
        self._cancellation_tokens.pop(
            (child.id, child.generation), None,
        )
