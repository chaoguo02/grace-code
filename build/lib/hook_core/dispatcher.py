"""
CC-aligned HookDispatcher — match, execute, merge.

Precedence: deny > defer > ask > allow (CC four-way permission model).
Deny short-circuits immediately.  Defer passes to next hook.
Transform results are merged from all hooks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from hook_core.decisions import (
    PermissionDecision,
    PreToolUseDecision, PostToolUseDecision, PostToolUseFailureDecision,
    StopDecision, SessionStartDecision, PreCompactDecision,
    UserPromptSubmitDecision, ObserveDecision,
)
from hook_core.events import BLOCKABLE_EVENTS
from hook_core.executor import execute_hook, HookExecution
from hook_core.policies import (
    HookPolicy, FailurePolicy,
    policy_for, PRETOOL_USE,
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
    # PreToolUse: final permission after merging all hooks
    permission: PermissionDecision | None = None
    # Merged transform data
    updated_input: dict | None = None
    additional_context: str = ""
    replace_output: str | None = None
    # Warnings from non-blocking failures
    warnings: list[str] = field(default_factory=list)

    @property
    def merged_decision(self) -> object | None:
        """Return the final typed decision for backward compatibility."""
        results = [r for r in self.results if r.decision is not None]
        return results[-1].decision if results else None


class HookDispatcher:
    """Awaited hook gate — dispatches to matching hooks, merges results.

    Deny always overrides allow.  Defer passes decision to next hook.
    """

    TOTAL_DEADLINE_S = 30.0

    def __init__(self, registry) -> None:
        self._registry = registry

    def dispatch(
        self,
        event_type: str,
        hook_input: object,
        *,
        snapshot: RegistrySnapshot | None = None,
        tool_name: str = "",
    ) -> DispatchResult:
        """Dispatch to matching hooks, merge results."""
        started = time.monotonic()
        policy = policy_for(event_type)
        hooks = self._registry.get_hooks(snapshot, event_type, tool_name)
        is_blockable = event_type in {e.value for e in BLOCKABLE_EVENTS}

        result = DispatchResult()
        permission: PermissionDecision | None = None

        for hook in hooks:
            elapsed = time.monotonic() - started
            if elapsed > self.TOTAL_DEADLINE_S:
                raise HookDispatchTimeout(
                    f"Total dispatch deadline {self.TOTAL_DEADLINE_S}s exceeded"
                )

            # ── stop_hook_active guard ──────────────────────────────
            if event_type == "Stop" and getattr(hook_input, "stop_hook_active", False):
                continue  # never block when already in forced continuation

            # ── Execute ─────────────────────────────────────────────
            execution = execute_hook(hook.name, hook.handler, hook_input, policy)
            result.results.append(execution)

            # ── Handle failure ──────────────────────────────────────
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
                    continue

            decision = execution.decision

            # ── Process decision by event type ──────────────────────
            if event_type == "PreToolUse" and isinstance(decision, PreToolUseDecision):
                perm = decision.permission
                # Deny → immediate block
                if perm == PermissionDecision.DENY:
                    result.blocked = True
                    result.block_reason = decision.reason or f"Hook '{hook.name}' denied"
                    result.permission = PermissionDecision.DENY
                    return result
                # Track highest-precedence permission
                permission = _merge_permission(permission, perm)
                # Merge transforms
                if decision.updated_input:
                    result.updated_input = {
                        **(result.updated_input or {}),
                        **decision.updated_input,
                    }
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
                        result.block_reason = decision.reason or result.block_reason
                    if getattr(decision, "additional_context", ""):
                        sep = "\n" if result.additional_context else ""
                        result.additional_context += sep + decision.additional_context
                    if getattr(decision, "replace_output", None) is not None:
                        result.replace_output = decision.replace_output

            elif event_type == "UserPromptSubmit" and isinstance(decision, UserPromptSubmitDecision):
                if decision.block:
                    result.blocked = True
                    result.block_reason = decision.reason or f"Hook '{hook.name}' blocked prompt"
                    return result
                if decision.updated_input:
                    result.updated_input = decision.updated_input

            elif event_type == "SessionStart" and isinstance(decision, SessionStartDecision):
                if decision.additional_context:
                    sep = "\n" if result.additional_context else ""
                    result.additional_context += sep + decision.additional_context

            elif event_type == "PreCompact" and isinstance(decision, PreCompactDecision):
                if decision.block:
                    result.blocked = True
                    result.block_reason = decision.reason or f"Hook '{hook.name}' blocked compaction"
                    return result

        # ── Finalize ────────────────────────────────────────────────
        result.total_duration_ms = (time.monotonic() - started) * 1000
        if event_type == "PreToolUse":
            result.permission = permission or PermissionDecision.ALLOW
        return result


def _merge_permission(current: PermissionDecision | None,
                      incoming: PermissionDecision) -> PermissionDecision | None:
    """Merge two permission decisions with defer semantics.

    Defer means "I don't decide — pass to the next hook."
    Otherwise, highest precedence wins: deny > defer > ask > allow.
    """
    if current is None:
        return incoming
    # Defer passes the decision to subsequent hooks
    if incoming == PermissionDecision.DEFER:
        return current
    if current == PermissionDecision.DEFER:
        return incoming
    # Both are concrete decisions — highest precedence wins
    prec = PermissionDecision.precedence()
    cur_idx = prec.index(current)
    inc_idx = prec.index(incoming)
    return current if cur_idx <= inc_idx else incoming


def _resolve_failure(failure_policy: FailurePolicy, is_blockable: bool) -> bool:
    """Return True if a hook failure should block the operation."""
    if failure_policy == FailurePolicy.FAIL_CLOSED:
        return True
    if failure_policy == FailurePolicy.EVENT_DEFAULT:
        return is_blockable
    if failure_policy == FailurePolicy.FAIL_TURN:
        return is_blockable
    return False  # FAIL_OPEN
