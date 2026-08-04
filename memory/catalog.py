"""Memory catalog generation — CC MEMORY.md equivalent.

Grace Code uses SQLite instead of file-system .md files, but the catalog
format is identical to CC's MEMORY.md: name, description, type grouped
by category, injected into system prompt once per session.

Post-compaction, the catalog is regenerated from SQLite so the LLM
always sees the current active memory set.

Usage:
    from memory.catalog import build_memory_catalog
    catalog_text = build_memory_catalog(store)
    # → "# Project Memory Index\n\n## user\n- `pref`: desc (user)\n..."
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memory.store import MemoryStore

logger = logging.getLogger(__name__)

# CC constraints
_MAX_LINES = 200
_MAX_BYTES = 25_000
_MAX_PER_TYPE = 15
_TYPE_ORDER = ("user", "feedback", "project", "reference")


def build_memory_catalog(
    store: "MemoryStore",
    max_lines: int = _MAX_LINES,
    max_bytes: int = _MAX_BYTES,
) -> str:
    """Generate CC MEMORY.md style catalog from SQLite-backed MemoryStore.

    Format (CC-aligned):
        # Project Memory Index

        ## user
        - ``name``: description (type)

        ## feedback
        - ``name``: description (type)
        ...

    Constraints:
        - ≤200 lines total (CC: MEMORY.md ≤ 200 lines)
        - ≤25KB total (CC: MEMORY.md ≤ 25KB)
        - ≤15 entries per type (avoids overloading the LLM)
        - Only ``status='active'`` entries are listed (deprecated = invisible)

    The LLM sees this catalog in its system prompt and autonomously
    decides which entries to read via the ``memory_read`` tool.
    """
    try:
        summaries = store.list_memories()
    except Exception:
        logger.debug("Memory catalog: list_memories failed", exc_info=True)
        return ""

    from memory.models import MemoryStatus

    # Filter: only active memories
    active = [
        s for s in summaries
        if not hasattr(s, 'status') or getattr(s, 'status', None) != MemoryStatus.DEPRECATED
    ]

    if not active:
        return ""

    # Group by type
    by_type: dict[str, list] = {t: [] for t in _TYPE_ORDER}
    for s in active:
        t = str(getattr(s, 'type', 'project'))
        if t in by_type:
            by_type[t].append(s)

    # Build catalog
    lines = ["# Project Memory Index"]
    line_count = 1

    for type_name in _TYPE_ORDER:
        mems = by_type[type_name]
        if not mems:
            continue
        lines.append(f"")  # blank line before heading
        lines.append(f"## {type_name}")
        line_count += 2

        shown = 0
        for m in mems:
            if shown >= _MAX_PER_TYPE:
                remaining = len(mems) - shown
                lines.append(f"- ... ({remaining} more `{type_name}` memories)")
                line_count += 1
                break
            # CC format: `name`: description
            desc = (getattr(m, 'description', '') or '')[:120]
            desc = desc.replace("\n", " ").strip()
            lines.append(f"- `{m.name}`: {desc}")
            shown += 1
            line_count += 1

        if line_count >= max_lines - 5:
            lines.append("")
            lines.append("... [truncated at line limit]")
            break

    content = "\n".join(lines).rstrip()
    if not content:
        return ""

    # Truncate at byte limit
    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        # Find safe cut point ≤ max_bytes
        truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
        content = truncated + "\n... [truncated at 25KB]"

    return content
