"""Structured tool result compression — extract facts, truncate body, render.

Phase 1 #1: Replaces character-level ``truncate_output()`` with semantic-level
``compress_tool_result()`` that extracts structured metadata from
``Observation.metadata`` before truncation.

Pure presentation layer — no changes to ``ToolResult`` or ``Observation``.

Design: docs/TOOL_SYSTEM_NORMALIZATION_DESIGN.md, Section 4.1 #1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.task import Observation

# ── Tier constants ───────────────────────────────────────────────────

COMPRESS_FULL_CHARS = 4_000      # below this, return as-is
COMPRESS_HEAD_CHARS = 4_000      # head chars in tier 1/2
COMPRESS_TAIL_CHARS = 4_000      # tail chars in tier 1/2 (tail is NEVER dropped)
COMPRESS_MAX_CHARS = 16_000      # above this, tier 2 (head%+tail%+facts)


# ── Facts dataclass ──────────────────────────────────────────────────


@dataclass
class ToolResultFacts:
    """Structural metadata extracted BEFORE truncation.

    All fields are read from ``Observation.metadata`` explicit keys.
    **Zero regex or string-pattern matching on output text** — facts
    must be populated upstream by tool executors.
    """

    exit_code: str = ""                    # from metadata["exit_code"]
    file_paths: tuple[str, ...] = ()       # from modified_files
    match_count: str = ""                  # from metadata["match_count"]
    error_lines: tuple[str, ...] = ()      # from metadata["error_lines"]
    preview: str = ""                      # first 200 chars of output

    @property
    def has_any(self) -> bool:
        return bool(self.exit_code or self.file_paths or self.match_count
                    or self.error_lines)

    def render(self) -> str:
        """Render facts block as compact model-visible text."""
        parts: list[str] = []
        if self.exit_code:
            parts.append(f"exit_code={self.exit_code}")
        if self.file_paths:
            paths_str = ", ".join(self.file_paths[:5])
            if len(self.file_paths) > 5:
                paths_str += f" and {len(self.file_paths) - 5} more"
            label = "files" if len(self.file_paths) != 1 else "file"
            parts.append(f"{label}: {paths_str}")
        if self.match_count:
            parts.append(self.match_count)
        if self.error_lines:
            parts.append(f"errors: {len(self.error_lines)}")
        return " | ".join(parts) if parts else ""


# ── Public API ───────────────────────────────────────────────────────


def compress_tool_result(observation: "Observation") -> str:
    """Extract facts, truncate body, render for model context.

    Replaces ``truncate_output(observation.output)`` in
    ``build_tool_result_content()`` and ``format_observations_for_history()``.
    """
    # 1. Empty result semantics
    empty_marker = _empty_result_text(observation)
    if empty_marker:
        return empty_marker

    output = observation.output or ""
    facts = _extract_facts(observation, output)

    # 2. Compress body
    body = _compress_body(output)

    # 3. Assemble
    parts: list[str] = []
    if facts.has_any:
        parts.append(facts.render())
    if body:
        parts.append(body)
    if observation.error and not observation.is_success():
        parts.append(f"Error: {observation.error}")

    return "\n".join(parts) if parts else "(no output)"


# ── Empty result semantics ───────────────────────────────────────────


def _empty_result_text(obs: "Observation") -> str | None:
    output = (obs.output or "").strip()
    if output:
        return None

    from core.types import ToolOutcome
    outcome = getattr(obs, "outcome", ToolOutcome.NONE)

    if outcome is ToolOutcome.EMPTY:
        return f"(no output — expected for {obs.tool_name})"
    if outcome is ToolOutcome.BLOCKED:
        error = obs.error or "blocked by policy"
        return f"(blocked: {error})"
    if outcome is ToolOutcome.SKIPPED:
        error = obs.error or "skipped"
        return f"(skipped — {error})"
    if outcome is ToolOutcome.PARTIAL:
        return "(partial output — content truncated)"
    if outcome is ToolOutcome.FAILED:
        error = obs.error or "tool execution failed"
        return f"(failed: {error})"

    return "(no output)"


# ── Fact extraction — reads ONLY from Observation.metadata ───────────


def _extract_facts(obs: "Observation", output: str) -> ToolResultFacts:
    """Extract structural facts from ``Observation.metadata``.

    Design contract: Zero regex or string-pattern matching on *output*.
    All facts come from explicit metadata keys populated by tool executors.
    If a key is absent, the fact is empty — the render layer does NOT
    compensate with heuristics.  Fix the executor, not the renderer.
    """
    metadata = getattr(obs, "metadata", None) or {}

    facts = ToolResultFacts()

    # exit_code — Bash/Shell executor writes this
    exit_code = metadata.get("exit_code")
    if exit_code is not None:
        facts.exit_code = str(exit_code)

    # file_paths — from modified_files (all write tools set this)
    modified = getattr(obs, "modified_files", None) or []
    facts.file_paths = tuple(modified[:10])

    # match_count — Grep/Glob executor writes this
    match_count = metadata.get("match_count")
    if match_count is not None:
        facts.match_count = str(match_count)

    # error_lines — tool executor extracts before truncation
    error_lines = metadata.get("error_lines")
    if isinstance(error_lines, (list, tuple)):
        facts.error_lines = tuple(str(line)[:200] for line in error_lines[:5])

    # preview — first 200 chars of raw output
    lines = [line.strip() for line in output.split("\n") if line.strip()]
    facts.preview = " | ".join(lines[:3])[:200]

    return facts


# ── Body compression ─────────────────────────────────────────────────


def _compress_body(output: str) -> str:
    """Apply tiered truncation.  Tail is NEVER dropped."""
    if len(output) <= COMPRESS_FULL_CHARS:
        return output

    if len(output) <= COMPRESS_MAX_CHARS:
        return _head_tail_truncate(output)

    return _head_tail_percent_truncate(output)


def _head_tail_truncate(output: str) -> str:
    """Tier 1: 50% head + 50% tail with omission marker."""
    head = output[:COMPRESS_HEAD_CHARS]
    tail = output[-COMPRESS_TAIL_CHARS:]
    omitted = len(output) - COMPRESS_HEAD_CHARS - COMPRESS_TAIL_CHARS
    return (
        f"{head}\n\n... [{omitted} chars omitted — "
        f"use targeted read for complete content] ...\n\n{tail}"
    )


def _head_tail_percent_truncate(output: str) -> str:
    """Tier 2: head (40%) + tail (40%) + metadata footer.  Tail never dropped."""
    head_chars = len(output) * 40 // 100
    tail_chars = len(output) * 40 // 100
    head = output[:head_chars]
    tail = output[-tail_chars:]
    omitted = len(output) - head_chars - tail_chars
    return (
        f"{head}\n\n... [{omitted} chars omitted — "
        f"use targeted read or grep for specific sections] ...\n\n{tail}"
    )
