"""
memory/selector.py

Lightweight LLM-based memory selector (aligned with Claude Code's findRelevantMemories).

Uses a Sonnet-class side-query to pick which project/reference memories
are relevant to the current conversation. Max 5 selected, max_tokens=256.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from llm.base import LLMBackend

logger = logging.getLogger(__name__)

# Verified real CC source (more complete version from source [10])
SELECT_MEMORIES_SYSTEM_PROMPT = (
    "You are selecting memories that will be useful to Claude Code.\n"
    "Return a list of filenames for the memories that will clearly\n"
    "be useful (up to 5).\n"
    "- If you are unsure if a memory will be useful, do not include it.\n"
    "- If a list of recently-used tools is provided, do not select\n"
    "  memories that are usage reference for those tools. DO still\n"
    "  select memories containing warnings, gotchas, or known issues."
)

_MAX_SELECTED = 5
_MAX_SCAN_FILES = 200
_FRONTMATTER_LINES = 30


@dataclass
class MemoryHeader:
    """Lightweight header extracted from a memory file's frontmatter."""
    filename: str
    description: str
    type: str
    mtime_ms: float


def scan_memory_headers(memory_dir: Path) -> list[MemoryHeader]:
    """
    Scan memory directory, reading only the first 30 lines of each .md file.

    Returns headers sorted by mtime descending, capped at 200 files.
    """
    if not memory_dir.exists():
        return []

    headers: list[MemoryHeader] = []
    for fpath in memory_dir.glob("*.md"):
        if fpath.name == "MEMORY.md" or fpath.name.startswith("."):
            continue
        try:
            mtime_ms = os.path.getmtime(fpath) * 1000
            # Only read first 30 lines (frontmatter area)
            with open(fpath, "r", encoding="utf-8") as f:
                lines = []
                for i, line in enumerate(f):
                    if i >= _FRONTMATTER_LINES:
                        break
                    lines.append(line)
            text = "".join(lines)
            header = _parse_header(fpath.stem, text, mtime_ms)
            if header:
                headers.append(header)
        except (OSError, UnicodeDecodeError):
            continue

    # Sort by mtime descending, cap at 200
    headers.sort(key=lambda h: h.mtime_ms, reverse=True)
    return headers[:_MAX_SCAN_FILES]


def build_manifest(headers: list[MemoryHeader], already_surfaced: set[str] | None = None) -> str:
    """
    Build the text manifest to send to the selector LLM.

    Format per line: - [type] filename (age): description
    """
    lines: list[str] = []
    for h in headers:
        if already_surfaced and h.filename in already_surfaced:
            continue
        age = _format_age(h.mtime_ms)
        lines.append(f"- [{h.type}] {h.filename} ({age}): {h.description}")
    return "\n".join(lines)


def parse_selection_response(response_text: str) -> list[str]:
    """
    Parse the selector LLM's response into a list of filenames.

    Handles JSON format ({"selected_memories": [...]}) and plain text lists.
    """
    # Try JSON first
    try:
        data = json.loads(response_text)
        if isinstance(data, dict):
            filenames = data.get("selected_memories") or data.get("filenames") or data.get("memories") or []
            if isinstance(filenames, list):
                return [f.strip() for f in filenames if isinstance(f, str)][:_MAX_SELECTED]
        if isinstance(data, list):
            return [f.strip() for f in data if isinstance(f, str)][:_MAX_SELECTED]
    except (json.JSONDecodeError, TypeError):
        pass

    # Fallback: extract filenames from text (lines that look like filenames)
    filenames: list[str] = []
    for line in response_text.splitlines():
        line = line.strip().lstrip("- ").strip("`\"'")
        if line and re.match(r"^[\w.-]+\.md$", line):
            filenames.append(line[:-3])
        elif line and re.match(r"^[\w.-]+$", line):
            filenames.append(line)
    return filenames[:_MAX_SELECTED]


def select_memories(
    query: str,
    memory_dir: Path,
    selector_backend: "LLMBackend",
    already_surfaced: set[str] | None = None,
    recent_tools: list[str] | None = None,
) -> list[str]:
    """
    Run the full selection pipeline: scan → manifest → LLM select → return filenames.

    Returns up to 5 memory names (without .md extension) that the selector chose.
    Returns empty list on any error (fail-open).
    """
    from llm.base import LLMMessage

    try:
        headers = scan_memory_headers(memory_dir)
        if not headers:
            return []

        # Filter to only on-demand types (project/reference)
        from memory.models import ON_DEMAND_TYPES
        on_demand_headers = [h for h in headers if h.type in ON_DEMAND_TYPES]
        if not on_demand_headers:
            return []

        manifest = build_manifest(on_demand_headers, already_surfaced)
        if not manifest.strip():
            return []

        # Build user message
        tools_section = ""
        if recent_tools:
            tools_section = f"\n\nRecently used tools: {', '.join(recent_tools)}"

        user_content = f"Query: {query}{tools_section}\n\nAvailable memories:\n{manifest}"

        messages = [
            LLMMessage(role="system", content=SELECT_MEMORIES_SYSTEM_PROMPT),
            LLMMessage(role="user", content=user_content),
        ]

        response = selector_backend.complete(messages, tools=[])
        if not response or not response.text:
            return []

        return parse_selection_response(response.text)

    except Exception as exc:
        logger.debug("Memory selector failed (fail-open): %s", exc)
        return []


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_header(name: str, text: str, mtime_ms: float) -> MemoryHeader | None:
    """Parse YAML frontmatter from the first 30 lines to extract description and type."""
    import yaml

    fm_match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not fm_match:
        return None
    try:
        fm = yaml.safe_load(fm_match.group(1)) or {}
    except Exception:
        return None

    from memory.models import parse_memory_type
    return MemoryHeader(
        filename=name,
        description=fm.get("description", ""),
        type=parse_memory_type(fm),
        mtime_ms=mtime_ms,
    )


def _format_age(mtime_ms: float) -> str:
    """Format file age as a human-readable relative time."""
    import time
    age_seconds = time.time() - (mtime_ms / 1000)
    days = int(age_seconds / 86400)
    if days <= 0:
        return "today"
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"
