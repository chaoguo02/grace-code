"""core/errors.py

Structured tool error types — extracted from core/base.py.
core/base.py re-exports all symbols for backward compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ToolErrorType(str, Enum):
    """Stable machine-readable categories for tool failures."""
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"
    ENVIRONMENT_UNAVAILABLE = "environment_unavailable"
    PROCESS_FAILED = "process_failed"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    INTERNAL = "internal"
    INVALID_PARAMS = "invalid_params"
    UNAVAILABLE = "unavailable"


class ToolRetryDirective(str, Enum):
    """Explicit Runtime guidance; never inferred from diagnostic prose."""
    RETRY = "retry"
    DO_NOT_RETRY = "do_not_retry"


@dataclass(frozen=True)
class ToolError:
    """Structured error from tool execution.

    Unlike raw string errors, this gives the Runtime and LLM enough
    information to decide: should I retry? Is there an alternative tool?
    """
    error_type: ToolErrorType
    retry: ToolRetryDirective = ToolRetryDirective.DO_NOT_RETRY
    alternative: str = ""
    detail: str = ""

    def to_message(self) -> str:
        parts = [f"[{self.error_type.value}]"]
        if self.detail:
            parts.append(f" {self.detail}")
        if self.retry is ToolRetryDirective.RETRY:
            parts.append(" (retryable)")
        if self.alternative:
            parts.append(f" (try '{self.alternative}' instead)")
        return "".join(parts)
