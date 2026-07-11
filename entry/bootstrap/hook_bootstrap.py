"""Hook dispatcher bootstrap — assembles the HookDispatcher with all
internal hooks (proactive memory, stop consolidation).

Constitution: hook assembly belongs in entry/bootstrap/ — it's wiring logic,
not CLI logic.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def init_hook_dispatcher(
    repo_path: Path,
    proactive_memory: Any = None,
    memory_store: Any = None,
    log_dir: str | None = None,
    backend: Any = None,
) -> Any:
    """Create HookDispatcher with ProactiveMemory + memory consolidation hooks."""
    from hooks import HookDispatcher, HookEvent, HookMatcher, HookRegistry, InternalHook

    registry = HookRegistry()
    settings_path = repo_path / ".forge-agent" / "settings.json"
    registry.load_from_settings(settings_path)

    if proactive_memory is not None:
        registry.register_internal(HookEvent.POST_TOOL_USE, InternalHook(
            callback=lambda ctx: proactive_memory.check_tool_result(
                ctx.tool_name, ctx.tool_input,
                (ctx.tool_output or {}).get("output", ""),
                (ctx.tool_output or {}).get("success", False),
            ),
            matcher=HookMatcher(pattern="shell"),
        ))
        registry.register_internal(HookEvent.POST_TOOL_USE, InternalHook(
            callback=lambda ctx: proactive_memory.notify_explicit_memory_write(),
            matcher=HookMatcher(pattern="memory_write"),
        ))

        def _on_user_prompt(ctx):
            proactive_memory.reset_turn()
            proactive_memory.check_user_message(ctx.user_input)

        registry.register_internal(HookEvent.USER_PROMPT_SUBMIT, InternalHook(
            callback=_on_user_prompt,
        ))

    if memory_store is not None:
        def _on_session_stop(ctx):
            from memory.consolidation import record_session_end, run_consolidation
            try:
                record_session_end(memory_store.store_dir)
                run_consolidation(memory_store, log_dir=log_dir, backend=backend, async_run=True)
            except Exception:
                pass
        registry.register_internal(HookEvent.STOP, InternalHook(callback=_on_session_stop))

    dispatcher = HookDispatcher(registry)
    return dispatcher
