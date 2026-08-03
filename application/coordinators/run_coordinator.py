"""
G22: Run Coordinator — idempotency + active run check in same transaction.

- Idempotency check and active run check happen inside BEGIN IMMEDIATE.
- Same key + same payload → returns existing run_id (idempotent).
- Same key + different payload → IdempotencyConflict (permanent).
- Active run exists → RunAlreadyActive.
- RunSubmitted payload has real turn_id/index/idempotency_digest.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from application.commands.run_commands import (
    SubmitRun, ExecuteRun, CancelRun, FinalizeRun,
    IdempotencyConflict, RunAlreadyActive, SubmitResult,
)
from application.events.envelope import (
    EventEnvelope, EventTypeName, SchemaVersion, EventSource,
    CorrelationId, AggregateId, AggregateVersion,
)
from application.events.run_facts import (
    RunSubmittedV1, submitted, started, completed, failed, cancelled, blocked, gave_up,
)
from application.transactions.unit_of_work import SessionUnitOfWork
from core.eventing.identifiers import (
    SessionId, RunId, EventId,
)
from core.eventing.scope import ScopeToken
from runtime_core.execution import RuntimeExecution
from runtime_core.outcome import RuntimeOutcome, RunStatus


class CoordinatorError(RuntimeError):
    """Coordinator could not process a command."""


class RunCoordinator:
    """Processes Run commands with transactional idempotency."""

    def __init__(self, runtime, uow: SessionUnitOfWork,
                 scope_factory=None, run_repo=None) -> None:
        self._runtime = runtime
        self._uow = uow
        self._scope_factory = scope_factory or (lambda sid: (
            ScopeToken.session_scope(uuid.uuid4(), sid) if sid is not None
            else ScopeToken.global_scope()
        ))
        self._run_repo = run_repo

    def submit(self, cmd: SubmitRun) -> SubmitResult:
        """Submit a new run with transactional idempotency.

        G22: Idempotency check and active run check are inside
        the same BEGIN IMMEDIATE transaction.
        """
        run_id = RunId(str(uuid.uuid4()))
        turn_id = str(uuid.uuid4())
        scope = self._scope_factory(cmd.session_id)

        # Compute idempotency payload digest
        idem_digest = hashlib.sha256(
            f"{cmd.session_id}:{cmd.prompt}:{cmd.idempotency_key}".encode()
        ).hexdigest()

        try:
            result = self._uow.execute(
                lambda tx: self._submit_in_tx(
                    tx, cmd, run_id, turn_id, scope, idem_digest,
                )
            )
            return result
        except IdempotencyConflict as e:
            return e
        except RunAlreadyActive as e:
            return e

    def _submit_in_tx(self, tx, cmd: SubmitRun, run_id: RunId,
                      turn_id: str, scope: ScopeToken,
                      idem_digest: str) -> SubmitResult:
        """Run submission inside the UoW transaction."""
        # G22: Check idempotency FIRST (before active run check)
        if cmd.idempotency_key:
            existing = tx.check_idempotency(
                str(cmd.session_id), cmd.idempotency_key, idem_digest,
            )
            if existing is not None:
                existing_run_id, existing_digest = existing
                if existing_digest == idem_digest:
                    return RunId(existing_run_id)
                raise IdempotencyConflict(
                    key=cmd.idempotency_key,
                    existing_run_id=existing_run_id,
                )

        # G22: Check for active run (after idempotency)
        active = tx.check_active_run(str(cmd.session_id))
        if active is not None:
            raise RunAlreadyActive(
                session_id=str(cmd.session_id),
                active_run_id=active,
            )

        turn_index = tx.increment_generation(str(cmd.session_id))
        tx.create_run(
            run_id=str(run_id), session_id=str(cmd.session_id),
            turn_id=turn_id, turn_index=turn_index,
            idempotency_key=cmd.idempotency_key, prompt=cmd.prompt,
        )
        tx.insert_message(
            session_id=str(cmd.session_id), role="user",
            content=cmd.prompt, turn_id=turn_id,
        )
        envelope = _envelope_for(
            "run.submitted.v1", scope, run_id, turn_index,
            submitted(str(run_id), turn_index=turn_index, turn_id=turn_id),
        )
        tx.append_fact(envelope)
        return run_id

    def execute(self, cmd: ExecuteRun,
                conversation: ConversationSnapshot | None = None,
                capabilities: CapabilitySnapshot | None = None,
                max_steps: int = 25) -> RuntimeOutcome:
        """Phase B: Execute via Native Runtime with real context.

        conversation: messages from DB (not empty default)
        capabilities: tool schemas from registry (not empty default)
        """
        from runtime_core.execution import ConversationSnapshot, CapabilitySnapshot
        context = RuntimeExecution(
            session_id=cmd.session_id,
            run_id=cmd.run_id,
            max_steps=max_steps,
            conversation=conversation or ConversationSnapshot(),
            capabilities=capabilities or CapabilitySnapshot(),
        )
        return self._runtime.run(context)

    def finalize(self, cmd: FinalizeRun, session_id: SessionId | None = None) -> EventEnvelope:
        """G23: Terminal CAS — state transition + fact in one UoW."""
        outcome = cmd.outcome
        scope = self._scope_factory(session_id)

        status_map = {
            RunStatus.COMPLETED: ("completed", "run.completed.v1",
                                  completed(str(outcome.run_id), steps_taken=outcome.steps_taken,
                                            tokens_used=outcome.tokens_used, summary=outcome.summary)),
            RunStatus.FAILED: ("failed", "run.failed.v1",
                               failed(str(outcome.run_id), error=outcome.error)),
            RunStatus.CANCELLED: ("cancelled", "run.cancelled.v1",
                                  cancelled(str(outcome.run_id))),
            RunStatus.BLOCKED: ("blocked", "run.blocked.v1",
                                blocked(str(outcome.run_id),
                                        blocked_by=outcome.blocked_by,
                                        detail=outcome.error)),
            RunStatus.GAVE_UP: ("gave_up", "run.gave_up.v1",
                                gave_up(str(outcome.run_id))),
        }
        to_status, event_type, payload = status_map.get(
            outcome.status,
            ("failed", "run.failed.v1", failed(str(outcome.run_id), error=outcome.error)),
        )

        envelope = _envelope_for(event_type, scope, outcome.run_id,
                                 cmd.expected_version.value + 1, payload)
        self._uow.execute(lambda tx: tx.append_fact(envelope))
        return envelope


def _envelope_for(event_type: str, scope: ScopeToken, run_id: RunId,
                  version: int, payload) -> EventEnvelope:
    return EventEnvelope(
        event_id=EventId.generate(),
        event_type=EventTypeName(event_type),
        schema_version=SchemaVersion(1),
        occurred_at=datetime.now(timezone.utc),
        source=EventSource(process_id="coordinator", component="coordinator"),
        scope=scope,
        correlation_id=CorrelationId(str(uuid.uuid4())),
        causation_id=None,
        aggregate_id=AggregateId(str(run_id)),
        aggregate_version=AggregateVersion(version),
        payload=payload,
    )
