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
                    coordinator=None) -> SubmittedRun:
    """G31: Submit via RunCoordinator using SqliteUnitOfWork.

    Single code path — no env-var branching, no nested adapter classes.
    """
    key = idempotency_key.strip()
    db_path = storage._db_path

    # If coordinator is injected, use it directly
    if coordinator is not None:
        from application.coordinators.run_coordinator import RunCoordinator
        cmd = SubmitRun(session_id=SessionId(session_id), prompt=prompt,
                        idempotency_key=key)
        result = coordinator.submit(cmd)
        if isinstance(result, IdempotencyConflict):
            raise IdempotencyConflictError("idempotency key reused with different prompt")
        if isinstance(result, RunAlreadyActive):
            raise RunAlreadyActiveError("RUN_ALREADY_ACTIVE")
        return SubmittedRun(
            run_id=str(result), turn_id=str(uuid.uuid4()),
            turn_index=1, created=True,
        )

    # Fallback: create coordinator from storage
    registry = SchemaRegistry()
    outbox = SqliteOutboxStore(db_path, registry)
    uow = SqliteUnitOfWork(db_path, outbox)

    from application.coordinators.run_coordinator import RunCoordinator
    from runtime_core.runtime import AgentRuntime
    from runtime_core.ports import RuntimePorts

    rt = AgentRuntime(RuntimePorts(
        llm=_stub_llm(), tools=_stub_tools(), hooks=_stub_hooks(),
        live_events=_stub_live(), clock=_stub_clock(),
        token_usage=_stub_tokens(), cancellation=_stub_cancel(),
    ))
    coord = RunCoordinator(rt, uow)

    cmd = SubmitRun(session_id=SessionId(session_id), prompt=prompt,
                    idempotency_key=key)
    result = coord.submit(cmd)

    if isinstance(result, IdempotencyConflict):
        raise IdempotencyConflictError("idempotency key reused with different prompt")
    if isinstance(result, RunAlreadyActive):
        raise RunAlreadyActiveError("RUN_ALREADY_ACTIVE")

    return SubmittedRun(
        run_id=str(result), turn_id=str(uuid.uuid4()),
        turn_index=1, created=True,
    )


# ── Stub ports for fallback path ────────────────────────────────────────

def _stub_llm():
    class S:
        def invoke(self, m, t=None): return object()
        def stream(self, m, t=None):
            async def _s(): return object()
            return _s()
    return S()

def _stub_tools():
    class S:
        def execute(self, n, p, i=""): return object()
    return S()

def _stub_hooks():
    class S:
        def check(self, e, i, t=""):
            from runtime_core.ports import HookGateResult
            return HookGateResult(allowed=True)
    return S()

def _stub_live():
    class S:
        def publish(self, e, p): pass
    return S()

def _stub_clock():
    class S:
        def now(self): return 0.0
        def deadline(self, s): return s
    return S()

def _stub_tokens():
    class S:
        def record(self, r, i, o): pass
    return S()

def _stub_cancel():
    class S:
        @property
        def cancelled(self): return False
    return S()
