"""
P14: Run Coordinator — processes commands, delegates to Runtime, persists via UoW.

Terminal state + fact in ONE Unit of Work transaction.
"""

from __future__ import annotations

import uuid

from application.commands.run_commands import (
    SubmitRun, ExecuteRun, CancelRun, FinalizeRun,
)
from application.events.envelope import (
    EventEnvelope, EventTypeName, SchemaVersion, EventSource,
    CorrelationId, AggregateId,
)
from application.events.run_facts import (
    RunSubmittedV1, RunStartedV1, RunCompletedV1, RunFailedV1, RunCancelledV1,
    submitted, started, completed, failed, cancelled,
)
from application.transactions.unit_of_work import SessionUnitOfWork
from core.eventing.identifiers import (
    SessionId, RunId, EventId, AggregateVersion,
)
from core.eventing.scope import ScopeToken
from runtime_core.execution import RuntimeExecution
from runtime_core.outcome import RuntimeOutcome, RunStatus


class CoordinatorError(RuntimeError):
    """Coordinator could not process a command."""


class RunCoordinator:
    """Processes Run commands.  Delegates to Runtime, persists atomically."""

    def __init__(
        self,
        runtime,
        uow: SessionUnitOfWork,
        scope_factory=None,  # callable(session_id) -> ScopeToken
    ) -> None:
        self._runtime = runtime
        self._uow = uow
        self._scope_factory = scope_factory or (lambda sid: (
            ScopeToken.session_scope(uuid.uuid4(), sid) if sid is not None
            else ScopeToken.global_scope()
        ))

    def submit(self, cmd: SubmitRun) -> EventEnvelope:
        """Submit a new run.  Creates Run + RunSubmitted fact in one UoW."""
        run_id = RunId(str(uuid.uuid4()))
        scope = self._scope_factory(cmd.session_id)
        envelope = _envelope_for(
            "run.submitted.v1", scope, run_id, 1,
            submitted(str(run_id)),
        )
        self._uow.execute(lambda tx: tx.append_fact(envelope))
        return envelope

    def execute(self, cmd: ExecuteRun) -> RuntimeOutcome:
        """Execute a run via Runtime."""
        context = RuntimeExecution(
            session_id=cmd.session_id, run_id=cmd.run_id,
        )
        return self._runtime.run(context)

    def finalize(self, cmd: FinalizeRun) -> EventEnvelope:
        """Persist terminal state + terminal fact in one UoW."""
        outcome = cmd.outcome
        scope = self._scope_factory(None)  # coordinator may not have session_id at finalize

        if outcome.status == RunStatus.COMPLETED:
            payload = completed(
                str(outcome.run_id), steps_taken=outcome.steps_taken,
                tokens_used=outcome.tokens_used, summary=outcome.summary,
            )
            event_type = "run.completed.v1"
        elif outcome.status == RunStatus.CANCELLED:
            payload = cancelled(str(outcome.run_id))
            event_type = "run.cancelled.v1"
        else:
            payload = failed(str(outcome.run_id), error=outcome.error)
            event_type = "run.failed.v1"

        envelope = _envelope_for(
            event_type, scope, outcome.run_id,
            cmd.expected_version.value, payload,
        )
        self._uow.execute(lambda tx: tx.append_fact(envelope))
        return envelope


def _envelope_for(event_type: str, scope: ScopeToken, run_id: RunId,
                  version: int, payload) -> EventEnvelope:
    return EventEnvelope(
        event_id=EventId.generate(),
        event_type=EventTypeName(event_type),
        schema_version=SchemaVersion(1),
        occurred_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        source=EventSource(process_id="coordinator", component="coordinator"),
        scope=scope,
        correlation_id=CorrelationId(str(uuid.uuid4())),
        causation_id=None,
        aggregate_id=AggregateId(str(run_id)),
        aggregate_version=AggregateVersion(version),
        payload=payload,
    )
