"""G31: Run submission — native coordinator only, single code path."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from core.eventing.identifiers import SessionId
from application.commands.run_commands import SubmitRun, IdempotencyConflict, RunAlreadyActive
from application.events.schema_registry import SchemaRegistry
from infrastructure.outbox.sqlite_store import SqliteOutboxStore
from infrastructure.sqlite.run_uow import SqliteUnitOfWork


class RunAlreadyActiveError(RuntimeError):
    pass


class IdempotencyConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class SubmittedRun:
    run_id: str
    turn_id: str
    turn_index: int
    created: bool


def submit_run_turn(storage, *, session_id: str, prompt: str,
                    idempotency_key: str = "",
                    coordinator) -> SubmittedRun:
    """Phase B: Submit via RunCoordinator.  Coordinator is REQUIRED.

    No fallback — the caller must inject a real coordinator.
    Single code path, no env-var branching.
    """
    key = idempotency_key.strip()

    # Check for existing idempotent run BEFORE coordinator call
    # The coordinator handles idempotency atomically, but we need to
    # detect idempotent vs new-creation for the SubmittedRun.created flag.
    if key:
        existing = storage.check_idempotent_run(session_id, key)
        if existing is not None:
            # Verify same prompt — conflict if different
            if existing.get("prompt", "") != prompt:
                raise IdempotencyConflictError(
                    "idempotency key reused with different prompt"
                )
            return SubmittedRun(
                run_id=existing["id"],
                turn_id=existing.get("turn_id", ""),
                turn_index=existing.get("turn_index", 1),
                created=False,
            )

    cmd = SubmitRun(session_id=SessionId(session_id), prompt=prompt,
                    idempotency_key=key)
    result = coordinator.submit(cmd)
    if isinstance(result, IdempotencyConflict):
        raise IdempotencyConflictError("idempotency key reused with different prompt")
    if isinstance(result, RunAlreadyActive):
        raise RunAlreadyActiveError("RUN_ALREADY_ACTIVE")

    # Get the turn_id from the persisted run (coordinator stores it)
    run = storage.get_run(str(result))
    turn_id = run["turn_id"] if run else str(uuid.uuid4())
    turn_index = run["turn_index"] if run else 1

    return SubmittedRun(
        run_id=str(result), turn_id=turn_id,
        turn_index=turn_index, created=True,
    )


# Phase B: All stub factories removed — coordinator is REQUIRED, no fallback.
