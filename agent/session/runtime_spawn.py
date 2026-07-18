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
    parent = self._store.get_session(parent_session_id)
    if parent is None:
        raise ValueError("Unknown v2 session: {parent_session_id}")
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
    child_contract = TaskContract.for_subagent(
        definition, self._root_agent_config,
        parent_budget_tokens=budget_tokens, parent_max_steps=parent_max_steps,
    )
    _repo = self._require_project_scope(parent.repo_path)
    child = self._store.create_session(
        agent_name=definition.name, mode=SessionMode.SUBAGENT,
        agent_kind=request.agent_kind, context_origin=request.context_origin,
        execution_placement=request.execution_placement,
        workspace_mode=request.workspace_mode, repo_path=_repo,
        title=request.description[:80] or definition.name,
        parent_id=parent.id, root_id=parent.root_id,
        metadata={"entrypoint": origin.value},
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
    execute = lambda: self._execute_child_session(
        parent=parent, child=child, request=request,
        definition=definition, parent_definition=parent_definition,
        contract=child_contract, cancellation_token=child_cancellation,
        parent_policy=parent_policy, repo_path=_repo,
        child_agent_type=child_agent_type, spawn_context=spawn_context,
    )
    if request.execution_placement is ExecutionPlacement.FOREGROUND:
        return execute()
    return self._start_background_execution(
        parent=parent, child=child, agent_name=definition.name, execute=execute,
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
        from agent.session.subagent import run_child_agent
        child_result = run_child_agent(
            agent_id=child.id, request=request, source_definition=definition,
            repo_path=repo_path, base_registry=self._base_registry,
            backend=self._backend, log_dir=self._log_dir,
            root_agent_config=self._root_agent_config, message_sink=_persist,
            contract=contract, cancellation_token=cancellation_token,
            parent_policy=parent_policy, spawn_context=spawn_context,
            inherited_registry=inherited_registry,
            event_callback=self._event_callback,
            persisted_messages=persisted_messages,
            session_record=child, session_runtime=self,
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
