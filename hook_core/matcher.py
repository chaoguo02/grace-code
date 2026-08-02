"""
CC-aligned hook matcher — NO regex, NO re module.

Syntax (permission-rule style, aligned with Claude Code):
  "*" / ""              → all tools
  "Bash"                → exact match on tool name
  "Edit|Write"          → pipe-separated OR list
  "mcp__server__*"      → prefix wildcard (only trailing *)

MCP tools follow convention:  mcp__<server>__<tool>
Subagent tool name:           Task
Skill tool name:              Skill
"""

from __future__ import annotations

from dataclasses import dataclass


class MatcherCompileError(ValueError):
    """Pattern syntax is invalid — must be rejected at config-load time."""


@dataclass(frozen=True, slots=True)
class HookMatcher:
    """Compiled hook matcher — one pattern per instance."""

    pattern: str = "*"

    def __post_init__(self) -> None:
        _validate(self.pattern)

    def matches(self, tool_name: str) -> bool:
        """Return True if *tool_name* matches this matcher."""
        if not self.pattern or self.pattern == "*":
            return True
        for part in self.pattern.split("|"):
            part = part.strip()
            if not part:
                continue
            if part.endswith("*"):
                prefix = part[:-1]
                if tool_name.startswith(prefix):
                    return True
            elif part == tool_name:
                return True
        return False


@dataclass(frozen=True, slots=True)
class HookSelector:
    """Composite selector — OR of multiple matchers.

    An empty selector matches ALL tools.
    """

    matchers: tuple[HookMatcher, ...] = ()

    @classmethod
    def all_tools(cls) -> "HookSelector":
        return cls()

    @classmethod
    def matching(cls, *patterns: str) -> "HookSelector":
        return cls(matchers=tuple(HookMatcher(p) for p in patterns))

    def selects(self, tool_name: str) -> bool:
        if not self.matchers:
            return True  # all_tools()
        return any(m.matches(tool_name) for m in self.matchers)

    def __bool__(self) -> bool:
        return bool(self.matchers)


def _validate(pattern: str) -> None:
    """Reject patterns that look like regex."""
    if not pattern or pattern == "*":
        return
    for part in pattern.split("|"):
        part = part.strip()
        if not part or part == "*":
            continue
        for ch in ".^$+?{}[]()\\":
            if ch in part:
                raise MatcherCompileError(
                    f"Pattern '{part}' contains regex metacharacter '{ch}'. "
                    f"Use exact tool names, pipe-separated lists, or "
                    f"prefix wildcards (e.g. 'Edit|Write', 'mcp__server__*')."
                )
