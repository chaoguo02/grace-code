"""
hooks/dispatcher.py

Central hook dispatcher: event → match → execute → decide.

Flow:
1. Event fires (tool call, session start, etc.)
2. Registry finds matching hooks (internal + external)
3. Internal hooks run first (in-process, cheap)
4. External hooks run via Runtime (stdin JSON, stdout parsed)
5. Exit code determines outcome (0=allow, 2=block for blockable events)
"""

from __future__ import annotations

import logging
import time as _time
from pathlib import Path
from typing import Any

from hooks.events import BLOCKABLE_EVENTS, HookContext, HookEvent
from hooks.executor import execute_hook
from hooks.protocol import DispatchResult, ExitCode, HookControl
from hooks.registry import HookRegistry

logger = logging.getLogger(__name__)


class HookDispatcher:
    """
    Synchronous hook dispatcher.

    Fires internal hooks (Python callables) first, then external hooks
    (Runtime-managed commands). Short-circuits on block for blockable events.
    """

    def __init__(
        self,
        registry: HookRegistry,
        cwd: str | None = None,
        runtime: Any = None,
    ) -> None:
        self._registry = registry
        self._cwd = str(Path(cwd or Path.cwd()).resolve())
        if runtime is None:
            from core.process import LocalRuntime

            runtime = LocalRuntime(workspace_root=self._cwd)
        self._runtime = runtime

    def dispatch(self, event: HookEvent, context: HookContext) -> DispatchResult:
        """
        Dispatch an event to all matching hooks.

        For blockable events (PreToolUse, UserPromptSubmit): exits 2 → block.
        For non-blockable events: exit codes are logged but don't block.
        """
        return self._dispatch(event, context)

    def dispatch_stop(self, context: HookContext) -> DispatchResult:
        """Compatibility entrypoint; blockability belongs to HookEvent."""
        return self.dispatch(HookEvent.STOP, context)

    def _dispatch(
        self,
        event: HookEvent,
        context: HookContext,
    ) -> DispatchResult:
        if context.event is not event:
            raise ValueError("Hook context event does not match dispatch event")
        matcher_subject = context.matcher_subject
        tool_input = context.tool_input

        # Phase 1: Internal hooks (cheap, in-process)
        internal_hooks = self._registry.find_internal(
            event, matcher_subject, tool_input,
        )
        for hook in internal_hooks:
            try:
                hook.callback(context)
            except Exception as exc:
                logger.debug("Internal hook failed for %s: %s", event.value, exc)

        # Phase 2: External hooks (Runtime-managed process), scoped by agent_id
        agent_id = getattr(context, "agent_id", "") or getattr(context, "session_id", "")
        external_hooks = self._registry.find_external(
            event, matcher_subject, tool_input, agent_id=agent_id,
        )
        if not external_hooks:
            return DispatchResult()

        collected_context: list[str] = []
        collected_warnings: list[str] = []
        updated_input: dict[str, Any] | None = None
        is_blockable = event in BLOCKABLE_EVENTS
        _hook_start = _time.time()
        _MAX_TOTAL = 30.0  # total hook execution budget (P2-19)

        for hook_config in external_hooks:
            _elapsed = _time.time() - _hook_start
            if _elapsed > _MAX_TOTAL:
                logger.warning(
                    "Hook total time cap (%.0fs) exceeded — skipping remaining %d hooks",
                    _MAX_TOTAL, len(external_hooks),
                )
                break
            result = execute_hook(
                command=hook_config.command,
                context=context,
                timeout=hook_config.timeout,
                cwd=self._cwd,
                runtime=self._runtime,
            )

            # Exit 2 = block (only for blockable events or dispatch_stop)
            if result.control is HookControl.BLOCK and is_blockable:
                reason = ""
                if result.parsed and result.parsed.reason:
                    reason = result.parsed.reason
                return DispatchResult(
                    control=HookControl.BLOCK,
                    reason=reason or result.stderr or result.stdout or "Blocked by hook",
                )

            # Exit 0 with explicit approve decision
            if result.control is HookControl.APPROVE:
                return DispatchResult(control=HookControl.APPROVE)

            # Collect CC-aligned: updatedInput + additionalContext
            if result.parsed and result.parsed.updated_input:
                updated_input = {**(updated_input or {}), **result.parsed.updated_input}
            if result.context:
                collected_context.append(result.context)

            # CC-aligned: non-blocking error (exit != 0,2) → warning, don't block
            if result.control is HookControl.NON_BLOCKING_ERROR:
                warning = (
                    f"Hook {hook_config.command} warned: "
                    f"{result.stderr or 'exit ' + str(result.exit_code)}"
                )
                collected_warnings.append(warning)
                logger.warning(warning)

        return DispatchResult(
            additional_context="\n".join(collected_context) if collected_context else "",
            updated_input=updated_input,
            warnings=collected_warnings if collected_warnings else None,
        )
