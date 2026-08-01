"""
P5: SessionStart context injection hook.

Injects previous session summary and CLAUDE.md content into new
sessions.  This is a SessionStart (transform) hook — it can inject
initial context but cannot block session creation.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class SessionContextInjector:
    """Injects cross-session context on SessionStart.

    Reads previous session summary and project instructions,
    returning them as additional context for the first prompt.

    Usage:
        injector = SessionContextInjector(repo_path="/path/to/project")
        hook_dispatcher.register(HookEvent.SESSION_START, injector.on_session_start)
    """

    def __init__(self, repo_path: str = ".") -> None:
        self._repo_path = Path(repo_path).resolve()

    def on_session_start(self, ctx: object) -> dict | None:
        """Return additional context to inject, or None."""
        context_parts: list[str] = []

        # Project instructions (CLAUDE.md)
        claude_md = self._repo_path / "CLAUDE.md"
        if claude_md.is_file():
            try:
                content = claude_md.read_text(encoding="utf-8")
                context_parts.append(f"## Project Instructions\n{content}")
            except OSError as exc:
                logger.debug("Cannot read CLAUDE.md: %s", exc)

        if not context_parts:
            return None

        return {"additional_context": "\n\n".join(context_parts)}
