"""
P11: Hook executor — runs a single hook, enforces policy timeout and failure.

No dict return — always returns typed decision or raises.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from hook_core.policies import HookPolicy, FailurePolicy


class HookTimeoutError(RuntimeError):
    """Hook exceeded its policy timeout."""


class HookExecutionError(RuntimeError):
    """Hook raised an unexpected exception."""


@dataclass(frozen=True, slots=True)
class HookExecution:
    hook_name: str
    decision: object | None   # typed decision, or None on failure
    duration_ms: float
    error: str = ""
    timed_out: bool = False


def execute_hook(
    hook_name: str,
    handler: object,
    hook_input: object,
    policy: HookPolicy,
) -> HookExecution:
    """Execute one hook with policy enforcement.

    Returns HookExecution.  On failure, the decision is None.
    The caller decides how to handle failures based on FailurePolicy.
    """
    started = time.monotonic()

    try:
        result = handler(hook_input)
    except Exception as exc:
        duration_ms = (time.monotonic() - started) * 1000
        return HookExecution(
            hook_name=hook_name, decision=None,
            duration_ms=duration_ms, error=str(exc),
        )

    duration_ms = (time.monotonic() - started) * 1000
    if duration_ms / 1000 > policy.timeout_s:
        return HookExecution(
            hook_name=hook_name, decision=None,
            duration_ms=duration_ms, timed_out=True,
            error=f"Hook exceeded {policy.timeout_s}s timeout",
        )

    return HookExecution(
        hook_name=hook_name, decision=result,
        duration_ms=duration_ms,
    )
