"""Trace context propagation via ``contextvars.ContextVar``.

Replaces ad-hoc id stitching (``invocation_id`` on ``ToolResult``,
``session_id`` on ``HookContext``) with a single ``contextvars``-based
propagation mechanism that survives arbitrary call depth without
explicit parameter passing.

Observability backends (Langfuse, local debug, unit test) read
``TraceContext.current()`` and map fields to their own span/trace
attributes.  No backend is imported here — this is pure propagation,
not binding.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TraceContext:
    """Immutable trace identity propagated through ``ContextVar``.

    All fields default to ``""`` (not present).  Observability backends
    that don't need a particular field can ignore it.
    """

    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str = ""
    invocation_id: str = ""
    session_id: str = ""
    root_run_id: str = ""

    def child(self, *, span_id: str = "", invocation_id: str = "") -> "TraceContext":
        """Create a child context for a sub-span (tool call, hook dispatch)."""
        return TraceContext(
            trace_id=self.trace_id or span_id,
            span_id=span_id or "",
            parent_span_id=self.span_id or self.trace_id,
            invocation_id=invocation_id or self.invocation_id,
            session_id=self.session_id,
            root_run_id=self.root_run_id,
        )

    def to_dict(self) -> dict[str, str]:
        """Serialize to dict for external hook consumption (JSON→stdin)."""
        return {
            k: v for k, v in {
                "trace_id": self.trace_id,
                "span_id": self.span_id,
                "parent_span_id": self.parent_span_id,
                "invocation_id": self.invocation_id,
                "session_id": self.session_id,
                "root_run_id": self.root_run_id,
            }.items() if v
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TraceContext":
        """Deserialize from dict (external hook output, test fixture)."""
        return cls(
            trace_id=str(d.get("trace_id", "") or ""),
            span_id=str(d.get("span_id", "") or ""),
            parent_span_id=str(d.get("parent_span_id", "") or ""),
            invocation_id=str(d.get("invocation_id", "") or ""),
            session_id=str(d.get("session_id", "") or ""),
            root_run_id=str(d.get("root_run_id", "") or ""),
        )


_current_trace: ContextVar[TraceContext | None] = ContextVar(
    "trace_context", default=None,
)


@dataclass
class TraceScope:
    """Context manager that sets ``TraceContext`` for a code block.

    Usage:
        with TraceScope(trace) as scope:
            # TraceContext.current() returns trace inside this block
            ...
        # Restored to previous value after the block
    """

    _new: TraceContext
    _token: Any = field(default=None, init=False)

    def __enter__(self) -> "TraceScope":
        self._token = _current_trace.set(self._new)
        return self

    def __exit__(self, *args: Any) -> None:
        _current_trace.reset(self._token)
        self._token = None

    @property
    def trace(self) -> TraceContext:
        return self._new


# ── Public API ──────────────────────────────────────────────────────


def current() -> TraceContext:
    """Return the current trace context, or an empty default."""
    ctx = _current_trace.get()
    return ctx or TraceContext()


def set_current(ctx: TraceContext) -> TraceScope:
    """Push *ctx* onto the context-var stack; returns scope for ``with``."""
    return TraceScope(ctx)
