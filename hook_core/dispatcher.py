"""
P11: Hook Dispatcher — awaited gate, total deadline, deny-overrides.

Aggregates results from multiple hooks.  Deny always wins.
Only supports awaited gate (no background hooks in P11).
"""

from __future__ import annotations

from dataclasses import dataclass
import time

from hook_core.decisions import (
    HookDecision, StopDecision, PreToolUseDecision, PostToolUseDecision,
    UserPromptSubmitDecision,
    PreCompactDecision,
)
from hook_core.executor import execute_hook, HookExecution
from hook_core.policies import (
    HookPolicy, FailurePolicy, DecisionAuthority,
    PRETOOL_USE, POSTTOOL_USE, USER_PROMPT_SUBMIT, STOP, PRECOMPACT,
)
from hook_core.registry import RegistrySnapshot, HookRegistration


class HookDispatchTimeout(RuntimeError):
    """Total dispatch deadline exceeded."""


@dataclass(frozen=True, slots=True)
class DispatchResult:
    results: tuple[HookExecution, ...]
    total_duration_ms: float
    blocked: bool = False
    block_reason: str = ""
    merged_decision: object | None = None  # aggregated typed decision


class HookDispatcher:
    """Awaited hook gate — dispatches to matching hooks, merges results.

    Deny always overrides allow.
    Fail-closed hooks block the operation on failure.
    """

    TOTAL_DEADLINE_S = 30.0

    def __init__(self, registry) -> None:
        self._registry = registry

    def dispatch(
        self,
        snapshot: RegistrySnapshot | None,
        event_type: str,
        hook_input: object,
        *,
        tool_name: str = "",
    ) -> DispatchResult:
        """Dispatch to matching hooks, merge results.

        For PreToolUse: deny wins over allow over ask.
        For Stop: block wins over continue.
        """
        started = time.monotonic()
        hooks = self._registry.get_hooks(snapshot, event_type, tool_name)
        policy = _policy_for(event_type)

        results: list[HookExecution] = []
        blocked = False
        block_reason = ""

        for hook in hooks:
            elapsed = time.monotonic() - started
            if elapsed > self.TOTAL_DEADLINE_S:
                raise HookDispatchTimeout(
                    f"Total dispatch deadline {self.TOTAL_DEADLINE_S}s exceeded"
                )

            result = execute_hook(hook.name, hook.handler, hook_input, policy)
            results.append(result)

            # Check if this hook should block
            if policy.failure_policy == FailurePolicy.FAIL_CLOSED and result.decision is None:
                blocked = True
                block_reason = f"Hook '{hook.name}' failed (fail-closed): {result.error}"
                break

            # Check decision-based blocking
            if event_type == "PreToolUse" and result.decision is not None:
                d = result.decision
                if getattr(d, "permission", None) == HookDecision.DENY:
                    blocked = True
                    block_reason = getattr(d, "reason", f"Hook '{hook.name}' denied")
                    break

            if event_type == "Stop" and result.decision is not None:
                d = result.decision
                if getattr(d, "decision", None) == "block":
                    blocked = True
                    block_reason = getattr(d, "reason", f"Hook '{hook.name}' blocked stop")
                    break

        total_ms = (time.monotonic() - started) * 1000
        merged = _merge_decisions(event_type, results) if results and not blocked else None

        return DispatchResult(
            results=tuple(results),
            total_duration_ms=total_ms,
            blocked=blocked,
            block_reason=block_reason,
            merged_decision=merged,
        )


def _policy_for(event_type: str) -> HookPolicy:
    if event_type == "PreToolUse":
        return PRETOOL_USE
    if event_type == "PostToolUse":
        return POSTTOOL_USE
    if event_type == "UserPromptSubmit":
        return USER_PROMPT_SUBMIT
    if event_type in ("Stop", "SubagentStop"):
        return STOP
    if event_type == "PreCompact":
        return PRECOMPACT
    return HookPolicy(
        scheduling="awaited", decision_authority="observe",
        data_authority="observe", failure_policy="fail_open",
    )


def _merge_decisions(event_type: str, results: list[HookExecution]) -> object | None:
    for r in results:
        if r.decision is not None:
            return r.decision
    return None
