"""Memory injection service — assembles memory context for LLM prompts.

Constitution: memory/ owns "retrieval / ranking" and "对 context 暴露可注入的结果".
This module is the single entry point for building the memory section that gets
injected into system prompts. It replaces the scattered logic that was in
agent/core.py and agent/prompt.py.

Architecture:
  agent/ asks: "give me the memory context for this task"
  memory/ answers with a formatted string (or None)
  agent/ injects it without knowing how it was built
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_injection_context(
    memory_context: Any = None,
    skills_prompt: str = "",
    repo_path: str = ".",
    *,
    session_context: str | None = None,
) -> str | None:
    """Build the full long-term context for injection into the system prompt.

    Returns a formatted string ready for injection, or None if nothing to inject.

    Components (in order):
      1. Memory section (from memory_context.build_memory_section())
      2. Project rules (from .forge-agent/rules.md)
      3. Skills prompt
      4. Session context (completed tasks from prior rounds)
    """
    parts: list[str] = []

    # ── 1. Memory section ──
    if memory_context is not None and getattr(memory_context, "enabled", False):
        memory_section = memory_context.build_memory_section()
        if memory_section:
            parts.append(memory_section)

    # ── 2. Project rules ──
    import os as _os
    rules_path = _os.path.join(repo_path, ".forge-agent", "rules.md")
    try:
        if _os.path.isfile(rules_path):
            with open(rules_path, "r", encoding="utf-8") as f:
                rules = f.read().strip()
            if rules:
                parts.append(f"## Project Rules\n{rules}")
    except OSError:
        pass

    # ── 3. Skills prompt ──
    if skills_prompt:
        parts.append(skills_prompt)

    # ── 4. Session context (completed tasks) ──
    if session_context:
        parts.append(f"## Session Context (completed tasks)\n{session_context}")

    return "\n\n".join(parts) if parts else None
