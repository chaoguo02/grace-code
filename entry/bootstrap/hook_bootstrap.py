"""Hook dispatcher bootstrap — assembles the HookDispatcher with all
internal hooks (stop consolidation).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def init_hook_dispatcher(
    repo_path: Path,
    memory_store: Any = None,
    log_dir: str | None = None,
    backend: Any = None,
) -> Any:
    """Create HookDispatcher with memory consolidation hooks."""
    from hooks import HookDispatcher, HookEvent, HookMatcher, HookRegistry, InternalHook

    registry = HookRegistry()
    settings_path = repo_path / ".forge-agent" / "settings.json"
    registry.load_from_settings(settings_path)

    if memory_store is not None:
        def _on_session_stop(ctx):
            from memory.consolidation import record_session_end, run_consolidation
            try:
                record_session_end(memory_store.store_dir)
                run_consolidation(memory_store, log_dir=log_dir, backend=backend, async_run=True)
            except Exception:
                pass
        registry.register_internal(HookEvent.STOP, InternalHook(callback=_on_session_stop))

    dispatcher = HookDispatcher(registry, cwd=str(repo_path.resolve()))
    return dispatcher
