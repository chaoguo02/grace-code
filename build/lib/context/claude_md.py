"""CLAUDE.md discovery and loading.

Claude Code reads CLAUDE.md from the project root and user home to
inject project-level instructions into the system prompt.  This module
provides the same capability: discover, load, and merge CLAUDE.md
files from the project tree.

Discovery order (first found wins per directory; all are merged):
1. ``<project_root>/CLAUDE.md``       — project instructions
2. ``<project_root>/.claude/CLAUDE.md`` — project instructions (dot-dir)
3. ``<home>/.claude/CLAUDE.md``         — user-global instructions

The content is concatenated (project first, then user) and injected
as a ``## Project Instructions`` section in the system prompt.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Maximum characters loaded from CLAUDE.md files.
# CC caps at ~40K chars for project instructions.
_MAX_CHARS = 40_000


def _resolve_home() -> Path:
    """Return the user home directory."""
    return Path.home()


def _discover(project_root: str | Path) -> list[tuple[str, Path]]:
    """Return (label, path) tuples for all CLAUDE.md files to load.

    Project-level files come first; user-global file last.
    """
    root = Path(project_root).resolve()
    home = _resolve_home()
    candidates: list[tuple[str, Path]] = []

    # 1. Project root
    _proj_root = root / "CLAUDE.md"
    if _proj_root.is_file():
        candidates.append(("Project", _proj_root))

    # 2. Project .claude/CLAUDE.md
    _proj_dot = root / ".claude" / "CLAUDE.md"
    if _proj_dot.is_file():
        candidates.append(("Project (.claude)", _proj_dot))

    # 3. User ~/.claude/CLAUDE.md
    _user = home / ".claude" / "CLAUDE.md"
    if _user.is_file() and _user != _proj_dot:
        candidates.append(("User", _user))

    return candidates


def load(project_root: str | Path) -> str:
    """Discover and load all CLAUDE.md content for *project_root*.

    Returns merged markdown text, or ``""`` if no CLAUDE.md files exist.
    Content is truncated at ``_MAX_CHARS`` to bound prompt cost.
    """
    candidates = _discover(project_root)
    if not candidates:
        return ""

    sections: list[str] = []
    total_chars = 0

    for label, path in candidates:
        try:
            text = path.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("Failed to read CLAUDE.md %s: %s", path, exc)
            continue
        if not text:
            continue

        remaining = _MAX_CHARS - total_chars
        if remaining <= 0:
            break

        if len(text) > remaining:
            text = text[:remaining] + "\n... [truncated]"

        sections.append(f"<!-- {label} CLAUDE.md -->\n{text}")
        total_chars += len(text)
        logger.info(
            "CLAUDE.md loaded: %s (%d chars, %s)",
            path, len(text), label,
        )

    if not sections:
        return ""

    return "\n\n".join(sections)


def has_claude_md(project_root: str | Path) -> bool:
    """Return ``True`` if any CLAUDE.md file exists for *project_root*."""
    return bool(_discover(project_root))
