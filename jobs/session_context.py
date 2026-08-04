"""
P24: Session context injection — extracted from AgentService.

Standalone functions: no dependency on AgentService state.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def build_recovery_context(repo_path: str) -> str:
    """Build context to re-inject after compaction: GRACE.md (.grace/) + recent files.

    Phase 10: GRACE.md is the Grace Code equivalent of CC's CLAUDE.md.
    """
    import os as _os
    parts: list[str] = []
    root = Path(repo_path)

    # Phase 10: GRACE.md first (Grace Code native), CLAUDE.md as CC fallback
    grace_path = root / ".grace" / "GRACE.md"
    claude_path = root / "CLAUDE.md"
    if grace_path.is_file():
        try:
            content = grace_path.read_text(encoding="utf-8")[:3000]
            parts.append(f"## Project Instructions (GRACE.md)\n{content}")
        except Exception:
            pass
    elif claude_path.is_file():
        try:
            content = claude_path.read_text(encoding="utf-8")[:3000]
            parts.append(f"## Project Instructions (CLAUDE.md)\n{content}")
        except Exception:
            pass

    _SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                  ".forge-agent", ".grace", ".claude", ".mypy_cache",
                  ".pytest_cache", ".tox", "dist", "build", ".eggs"}
    try:
        recent: list[tuple[str, float]] = []
        for dirpath, dirnames, filenames in _os.walk(str(root)):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                fp = _os.path.join(dirpath, fn)
                try:
                    mtime = _os.path.getmtime(fp)
                    recent.append((fp, mtime))
                except OSError:
                    continue
        recent.sort(key=lambda x: x[1], reverse=True)
        if recent:
            parts.append("## Recently Modified Files")
            for fp, _ in recent[:5]:
                try:
                    content = Path(fp).read_text(encoding="utf-8")[:1000]
                    rel = str(Path(fp).relative_to(root))
                    parts.append(f"### {rel}\n{content}")
                except Exception:
                    pass
    except Exception:
        pass

    return "\n\n".join(parts)
