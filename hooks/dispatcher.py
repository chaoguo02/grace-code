"""
hooks/dispatcher.py

Central hook dispatcher: event → match → execute → decide.

Flow:
1. Event fires (tool call, session start, etc.)
2. Registry finds matching hooks (internal + external)
3. Internal hooks run first (in-process, cheap)
4. External hooks run via subprocess (stdin JSON, stdout parsed)
5. Exit code determines outcome (0=allow, 2=block for blockable events)
"""

from __future__ import annotations

import logging
from typing import Any

from hooks.events import BLOCKABLE_EVENTS, HookContext, HookEvent
from hooks.executor import execute_hook
from hooks.protocol import DispatchResult, ExitCode
from hooks.registry import HookRegistry

logger = logging.getLogger(__name__)


class HookDispatcher:
    """
    Synchronous hook dispatcher.

    Fires internal hooks (Python callables) first, then external hooks
    (subprocess commands). Short-circuits on block for blockable events.
    """

    def __init__(self, registry: HookRegistry, cwd: str | None = None) -> None:
        self._registry = registry
        self._cwd = cwd

    def dispatch(self, event: HookEvent, context: HookContext) -> DispatchResult:
        """
        Dispatch an event to all matching hooks.

        For blockable events (PreToolUse, UserPromptSubmit): exits 2 → block.
        For non-blockable events: exit codes are logged but don't block.
        """
        return self._dispatch(event, context, force_blockable=False)

    def dispatch_stop(self, context: HookContext) -> DispatchResult:
        """Dispatch Stop hooks with Claude Code-style blocking semantics."""
        return self._dispatch(HookEvent.STOP, context, force_blockable=True)

    def _dispatch(self, event: HookEvent, context: HookContext, *, force_blockable: bool) -> DispatchResult:
        tool_name = context.tool_name
        tool_input = context.tool_input

        # Phase 1: Internal hooks (cheap, no subprocess)
        internal_hooks = self._registry.find_internal(event, tool_name, tool_input)
        for hook in internal_hooks:
            try:
                hook.callback(context)
            except Exception as exc:
                logger.debug("Internal hook failed for %s: %s", event.value, exc)

        # Phase 2: External hooks (subprocess)
        external_hooks = self._registry.find_external(event, tool_name, tool_input)
        if not external_hooks:
            return DispatchResult()

        collected_context: list[str] = []
        is_blockable = force_blockable or event in BLOCKABLE_EVENTS

        for hook_config in external_hooks:
            result = execute_hook(
                command=hook_config.command,
                context=context,
                timeout=hook_config.timeout,
                cwd=self._cwd,
            )

            # Exit 2 = block (only for blockable events or dispatch_stop)
            if result.blocks and is_blockable:
                reason = ""
                if result.parsed and result.parsed.reason:
                    reason = result.parsed.reason
                return DispatchResult(
                    blocked=True,
                    reason=reason or result.stderr or result.stdout or "Blocked by hook",
                )

            # Exit 0 with explicit approve decision
            if result.approves_explicitly:
                return DispatchResult(approved_explicitly=True)

            # Collect additional context
            if result.has_context:
                if result.parsed and result.parsed.additional_context:
                    collected_context.append(result.parsed.additional_context)
                elif result.stdout:
                    collected_context.append(result.stdout)

            # Non-zero, non-2 exit = non-blocking error, log and continue
            if result.exit_code not in (ExitCode.SUCCESS, ExitCode.BLOCKING_ERROR):
                logger.debug(
                    "Hook %s exited %d for %s (non-blocking): %s",
                    hook_config.command, result.exit_code, event.value, result.stderr,
                )

        return DispatchResult(
            additional_context="\n".join(collected_context) if collected_context else "",
        )
