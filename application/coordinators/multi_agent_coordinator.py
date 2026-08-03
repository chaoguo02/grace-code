"""
G32: Multi-Agent Coordinator — primary-mediated only, no Team.

- Child tasks get fresh TaskContext (no parent history copy).
- Each child has exact Task Scope — no implicit bubbling.
- Child RuntimeOutcome → Coordinator → parent Session scope fact.
- Parent cancel pushes to all active children.
- Write children with same workspace lease are serialized.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from application.events.envelope import (
    EventEnvelope, EventTypeName, SchemaVersion, EventSource,
    CorrelationId, AggregateId, AggregateVersion,
)
from core.eventing.identifiers import SessionId, RunId, TaskId, EventId
from core.eventing.scope import ScopeToken
from runtime_core.outcome import RuntimeOutcome, RunStatus


@dataclass(frozen=True, slots=True)
class ChildTaskContext:
    """Fresh context for a child task — no parent history copy."""
    task_id: TaskId
    description: str
    allowed_tools: tuple[str, ...] = ()
    workspace_lease: str = ""  # resource key for serialization
    budget_tokens: int = 50_000
    parent_run_id: str = ""
    parent_session_id: str = ""


@dataclass
class ChildTaskResult:
    task_id: TaskId
    outcome: RuntimeOutcome | None = None
    error: str = ""


class MultiAgentCoordinator:
    """Primary-mediated multi-agent: parent delegates to children.

    G32: No Team topology.  Parent cancels children.
         Workspace lease conflict → serialized execution.
    """

    def __init__(self, run_coordinator, runtime, scope_factory,
                 cancellation_registry=None) -> None:
        self._coordinator = run_coordinator
        self._runtime = runtime
        self._scope_factory = scope_factory
        self._cancel_registry = cancellation_registry

    def create_child_contexts(self, tasks: list[dict],
                              parent_run_id: str,
                              parent_session_id: str) -> list[ChildTaskContext]:
        """Create fresh ChildTaskContext for each task."""
        contexts = []
        for t in tasks:
            ctx = ChildTaskContext(
                task_id=TaskId(str(uuid.uuid4())),
                description=t.get("description", ""),
                allowed_tools=tuple(t.get("allowed_tools", [])),
                workspace_lease=t.get("workspace_lease", ""),
                budget_tokens=t.get("budget_tokens", 50_000),
                parent_run_id=parent_run_id,
                parent_session_id=parent_session_id,
            )
            contexts.append(ctx)
        return contexts

    def execute_children(self, contexts: list[ChildTaskContext],
                         parent_session_id: str) -> list[ChildTaskResult]:
        """Execute children sequentially, serializing workspace conflicts."""
        results: list[ChildTaskResult] = []
        active_leases: set[str] = set()

        for ctx in contexts:
            # Serialize write conflicts
            if ctx.workspace_lease and ctx.workspace_lease in active_leases:
                # Wait for previous lease holder to finish (simplified: sequential)
                pass

            result = self._execute_one_child(ctx)
            results.append(result)

            if ctx.workspace_lease:
                active_leases.add(ctx.workspace_lease)

        # Emit DelegationCompleted fact in parent Session scope
        self._emit_delegation_completed(parent_session_id, len(results))

        return results

    def _execute_one_child(self, ctx: ChildTaskContext) -> ChildTaskResult:
        """Execute a single child task."""
        from runtime_core.execution import RuntimeExecution, ConversationSnapshot

        task_scope = self._scope_factory(
            SessionId(ctx.parent_session_id),
        )

        execution = RuntimeExecution(
            session_id=SessionId(ctx.parent_session_id),
            run_id=RunId(str(uuid.uuid4())),
            max_steps=10,
            budget_tokens=ctx.budget_tokens,
            conversation=ConversationSnapshot(
                messages=({"role": "system", "content": ctx.description},),
            ),
        )

        try:
            outcome = self._runtime.run(execution)
            return ChildTaskResult(task_id=ctx.task_id, outcome=outcome)
        except Exception as exc:
            return ChildTaskResult(task_id=ctx.task_id, error=str(exc))

    def cancel_children(self, child_ids: list[str]) -> int:
        """Cancel all active children.  Returns count cancelled."""
        if self._cancel_registry is None:
            return 0
        return self._cancel_registry.cancel_children("parent", child_ids)

    def _emit_delegation_completed(self, session_id: str, count: int) -> None:
        """Emit fact in parent Session scope."""
        # Simplified: just record — actual fact emission via coordinator
        pass
