"""
G18: Runtime execution — frozen snapshot + CancellationHandle.

CancellationHandle allows external actors to cancel a running execution.
The step loop checks it at every async/await boundary.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from core.eventing.identifiers import SessionId, RunId


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    messages: tuple[dict, ...] = ()
    system_prompt: str = ""
    project_instructions: str = ""


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    tool_schemas: tuple[dict, ...] = ()
    active_skills: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()


# ── G18: CancellationHandle ────────────────────────────────────────────────

class CancellationHandle:
    """Thread-safe cancellation token for a single run execution.

    Usage:
        handle = CancellationHandle()
        execution = RuntimeExecution(..., cancellation=handle)
        # External actor:
        handle.cancel()

        # In step loop:
        if handle.cancelled:
            return RuntimeOutcome.cancelled(...)
    """

    # H6: Class-level default — set once at composition time
    _default_process_registry = None

    @classmethod
    def set_process_registry(cls, registry) -> None:
        cls._default_process_registry = registry

    def __init__(self, process_registry=None) -> None:
        self._cancelled = False
        self._lock = threading.Lock()
        # H6: Use passed registry or class-level default
        self._process_registry = process_registry or self._default_process_registry

    def cancel(self) -> None:
        """Signal cancellation.  Idempotent.  Also kills subprocesses (H6)."""
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
        # H6: Kill all in-flight hook/tool subprocesses
        if self._process_registry is not None:
            self._process_registry.cancel_all()

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def is_active(self) -> bool:
        """True if the handle can still be cancelled (not yet in terminal state)."""
        return not self._cancelled


# ── RuntimeExecution ────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class RuntimeExecution:
    """Immutable execution context for one run.

    G18: includes CancellationHandle for external cancellation.
    """
    session_id: SessionId
    run_id: RunId
    cancellation: CancellationHandle = field(default_factory=CancellationHandle)
    turn_index: int = 0
    conversation: ConversationSnapshot = field(default_factory=ConversationSnapshot)
    capabilities: CapabilitySnapshot = field(default_factory=CapabilitySnapshot)
    max_steps: int = 25
    budget_tokens: int = 200_000
