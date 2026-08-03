"""
G14: Async HookDispatcher — TaskGroup parallel, asyncio.timeout, typed merge.

- async dispatch(): matching hooks run concurrently via asyncio.TaskGroup.
- Per-hook and total deadlines via asyncio.timeout / asyncio.wait_for.
- PreToolUse precedence: deny > defer > ask > allow (deny short-circuits).
- Transform conflicts detected and reported (not silently overwritten).
- fail-open → HookWarning in result.warnings (live event candidate).
- fail-closed → immediate block.
- No daemon/background hooks.
"""

from __future__ import annotations

import asyncio
import time as _time
from dataclasses import dataclass, field

from core.json_values import FrozenJsonObject, freeze_json
from hook_core.decisions import (
    PermissionDecision,
    PreToolUseDecision, PostToolUseDecision, PostToolUseFailureDecision,
    StopDecision, SessionStartDecision, PreCompactDecision,
    UserPromptSubmitDecision,
)
from hook_core.events import BLOCKABLE_EVENTS, HookEvent
from hook_core.executor import (
    execute_hook, HookExecution,
    execute_hook_async,
)
from hook_core.policies import (
    HookPolicy, FailurePolicy,
    policy_for,
)
from hook_core.registry import RegistrySnapshot, HookRegistration


class HookDispatchTimeout(RuntimeError):
    """Total dispatch deadline exceeded."""


@dataclass
class DispatchResult:
    """Aggregated result from dispatching an event to matching hooks."""

    results: list[HookExecution] = field(default_factory=list)
    total_duration_ms: float = 0.0
    blocked: bool = False
    block_reason: str = ""
    permission: PermissionDecision | None = None
    updated_input: FrozenJsonObject | None = None  # G14: was dict | None
    additional_context: str = ""
    replace_output: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def merged_decision(self) -> object | None:
        results = [r for r in self.results if r.decision is not None]
        return results[-1].decision if results else None


class HookDispatcher:
    """Awaited async hook gate — dispatches, merges, enforces precedence.

    G14: async dispatch with TaskGroup parallelism.
    """

    TOTAL_DEADLINE_S = 30.0

    def __init__(self, registry) -> None:
        self._registry = registry

    # ── Sync dispatch (backward compat) ──────────────────────────────────

    def dispatch(
        self,
        event_type: str,
        hook_input: object,
        *,
        snapshot: RegistrySnapshot | None = None,
        tool_name: str = "",
    ) -> DispatchResult:
        """Sync dispatch — serial execution.  Kept for backward compat."""
        started = _time.monotonic()
        policy = policy_for(event_type)
        hooks = self._registry.get_hooks(snapshot, event_type, tool_name)
        is_blockable = event_type in {e.value for e in BLOCKABLE_EVENTS}

        result = DispatchResult()
        permission: PermissionDecision | None = None

        for hook in hooks:
            elapsed = _time.monotonic() - started
            if elapsed > self.TOTAL_DEADLINE_S:
                raise HookDispatchTimeout(
                    f"Total dispatch deadline {self.TOTAL_DEADLINE_S}s exceeded"
                )

            if event_type == "Stop" and getattr(hook_input, "stop_hook_active", False):
                continue

            execution = execute_hook(hook.name, hook.handler, hook_input, policy)
            result.results.append(execution)

            early_return = _process_execution(
                event_type, hook, execution, result, permission, policy, is_blockable,
            )
            if early_return is not None:
                return early_return
            if execution.decision is not None and event_type == "PreToolUse":
                dec = execution.decision
                if isinstance(dec, PreToolUseDecision):
                    permission = _merge_permission(permission, dec.permission)
                    result = _merge_transform(result, dec)

        result.total_duration_ms = (_time.monotonic() - started) * 1000
        if event_type == "PreToolUse":
            result.permission = permission or PermissionDecision.ALLOW
        return result

    # ── G14: Async dispatch ──────────────────────────────────────────────

    async def dispatch_async(
        self,
        event_type: str,
        hook_input: object,
        *,
        snapshot: RegistrySnapshot | None = None,
        tool_name: str = "",
    ) -> DispatchResult:
        """Async dispatch — hooks run concurrently via TaskGroup."""
        started = _time.monotonic()
        policy = policy_for(event_type)
        hooks = self._registry.get_hooks(snapshot, event_type, tool_name)
        is_blockable = event_type in {e.value for e in BLOCKABLE_EVENTS}

        if not hooks:
            return DispatchResult(total_duration_ms=0.0)

        result = DispatchResult()
        permission: PermissionDecision | None = None
        executions: dict[str, HookExecution] = {}

        async def run_hook(hook: HookRegistration) -> None:
            if event_type == "Stop" and getattr(hook_input, "stop_hook_active", False):
                return
            try:
                exec_result = await asyncio.wait_for(
                    execute_hook_async(hook.name, hook.handler, hook_input, policy),
                    timeout=min(policy.timeout_s, self.TOTAL_DEADLINE_S),
                )
            except asyncio.TimeoutError:
                exec_result = HookExecution(
                    hook_name=hook.name, decision=None, duration_ms=0.0,
                    timed_out=True, error="Async hook timed out",
                )
            executions[hook.name] = exec_result

        try:
            async with asyncio.TaskGroup() as tg:
                for hook in hooks:
                    tg.create_task(run_hook(hook))
        except* Exception:
            # TaskGroup collects exceptions from child tasks.
            # Individual hook errors are already in executions dict.
            pass

        # Process results in stable registration order
        for hook in hooks:
            execution = executions.get(hook.name)
            if execution is None:
                continue
            result.results.append(execution)

            early_return = _process_execution(
                event_type, hook, execution, result, permission, policy, is_blockable,
            )
            if early_return is not None:
                result.total_duration_ms = (_time.monotonic() - started) * 1000
                return early_return
            if execution.decision is not None and event_type == "PreToolUse":
                dec = execution.decision
                if isinstance(dec, PreToolUseDecision):
                    permission = _merge_permission(permission, dec.permission)
                    result = _merge_transform(result, dec)

        result.total_duration_ms = (_time.monotonic() - started) * 1000
        if event_type == "PreToolUse":
            result.permission = permission or PermissionDecision.ALLOW
        return result


# ── Execution processing (shared by sync + async) ─────────────────────────

def _process_execution(
    event_type: str,
    hook: HookRegistration,
    execution: HookExecution,
    result: DispatchResult,
    permission: PermissionDecision | None,
    policy: HookPolicy,
    is_blockable: bool,
) -> DispatchResult | None:
    """Process a single hook execution.  Returns DispatchResult on early exit."""
    if execution.decision is None:
        failure_blocks = _resolve_failure(policy.failure_policy, is_blockable)
        if failure_blocks:
            result.blocked = True
            result.block_reason = (
                f"Hook '{hook.name}' failed (fail-closed): {execution.error}"
            )
            return result
        else:
            result.warnings.append(
                f"Hook '{hook.name}' failed: {execution.error}"
            )
            return None

    decision = execution.decision

    if event_type == "PreToolUse" and isinstance(decision, PreToolUseDecision):
        perm = decision.permission
        if perm == PermissionDecision.DENY:
            result.blocked = True
            result.block_reason = decision.reason or f"Hook '{hook.name}' denied"
            result.permission = PermissionDecision.DENY
            return result
        if decision.reason and not result.block_reason:
            result.block_reason = decision.reason

    elif event_type == "Stop" and isinstance(decision, StopDecision):
        if decision.decision == "block":
            result.blocked = True
            result.block_reason = decision.reason or f"Hook '{hook.name}' blocked stop"
            return result

    elif event_type in ("PostToolUse", "PostToolUseFailure"):
        if isinstance(decision, (PostToolUseDecision, PostToolUseFailureDecision)):
            if decision.decision == "block":
                result.block_reason = result.block_reason or "blocked by hook"
            if getattr(decision, "additional_context", ""):
                sep = "\n" if result.additional_context else ""
                result.additional_context += sep + str(decision.additional_context)
            if getattr(decision, "replace_output", None) is not None:
                result.replace_output = str(decision.replace_output)

    elif event_type == "UserPromptSubmit" and isinstance(decision, UserPromptSubmitDecision):
        if decision.block:
            result.blocked = True
            result.block_reason = decision.reason or f"Hook '{hook.name}' blocked prompt"
            return result
        if decision.updated_input is not None:
            result.updated_input = _merge_updated_input(
                result.updated_input, decision.updated_input,
            )

    elif event_type == "SessionStart" and isinstance(decision, SessionStartDecision):
        if decision.additional_context:
            sep = "\n" if result.additional_context else ""
            result.additional_context += sep + decision.additional_context

    elif event_type == "PreCompact" and isinstance(decision, PreCompactDecision):
        if decision.block:
            result.blocked = True
            result.block_reason = decision.reason or f"Hook '{hook.name}' blocked compaction"
            return result

    return None


# ── Permission merge ────────────────────────────────────────────────────────

def _merge_permission(current: PermissionDecision | None,
                      incoming: PermissionDecision) -> PermissionDecision | None:
    """Merge two permission decisions with defer semantics."""
    if current is None:
        return incoming
    if incoming == PermissionDecision.DEFER:
        return current
    if current == PermissionDecision.DEFER:
        return incoming
    prec = PermissionDecision.precedence()
    cur_idx = prec.index(current)
    inc_idx = prec.index(incoming)
    return current if cur_idx <= inc_idx else incoming


# ── Transform merge (G14: FrozenJsonObject, conflict detection) ─────────────

def _merge_transform(result: DispatchResult, decision: PreToolUseDecision) -> DispatchResult:
    """Merge updated_input with conflict detection."""
    if decision.updated_input is None:
        return result
    if result.updated_input is None:
        result.updated_input = decision.updated_input
        return result
    # G14: Transform conflict — two hooks try to set different values for same key
    for key in decision.updated_input.keys():
        if key in result.updated_input:
            existing = result.updated_input[key]
            incoming = decision.updated_input[key]
            if existing != incoming:
                result.warnings.append(
                    f"Transform conflict on key '{key}': "
                    f"hook '{result.results[-1].hook_name if result.results else '?'}' "
                    f"overwrites previous value"
                )
    merged = freeze_json({
        **{k: result.updated_input[k] for k in result.updated_input.keys()},
        **{k: decision.updated_input[k] for k in decision.updated_input.keys()},
    })
    result.updated_input = merged
    return result


def _merge_updated_input(
    current: FrozenJsonObject | None,
    incoming: FrozenJsonObject,
) -> FrozenJsonObject:
    """Merge two updated_input objects (last write wins for conflicts)."""
    if current is None:
        return incoming
    merged = freeze_json({
        **{k: current[k] for k in current.keys()},
        **{k: incoming[k] for k in incoming.keys()},
    })
    return merged


# ── Failure resolution ──────────────────────────────────────────────────────

def _resolve_failure(failure_policy: FailurePolicy, is_blockable: bool) -> bool:
    """Return True if a hook failure should block the operation."""
    if failure_policy == FailurePolicy.FAIL_CLOSED:
        return True
    if failure_policy == FailurePolicy.EVENT_DEFAULT:
        return is_blockable
    if failure_policy == FailurePolicy.FAIL_TURN:
        return is_blockable
    return False  # FAIL_OPEN
