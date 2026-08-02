"""
G12: Hook matcher — exact/pipe-list/prefix-wildcard + safe compiled regex.

- Exact tool name, pipe-separated list, prefix wildcard (trailing *).
- Safe compiled regex via explicit opt-in: MatcherPattern(kind="regex", ...).
- Bad regex rejected at registration/compile time (MatcherCompileError).
- Regex patterns are validated for safety: no catastrophic backtracking operators.
"""

from __future__ import annotations

import re as _re
from dataclasses import dataclass
from enum import StrEnum


class MatcherCompileError(ValueError):
    """Pattern syntax is invalid — rejected at registration time."""


class PatternKind(StrEnum):
    EXACT = "exact"
    PREFIX = "prefix"
    REGEX = "regex"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class MatcherPattern:
    """A single match pattern with explicit kind."""
    kind: PatternKind
    value: str = ""

    def matches(self, tool_name: str) -> bool:
        if self.kind == PatternKind.ALL:
            return True
        if self.kind == PatternKind.EXACT:
            return tool_name == self.value
        if self.kind == PatternKind.PREFIX:
            return tool_name.startswith(self.value)
        if self.kind == PatternKind.REGEX:
            return bool(_re.fullmatch(self.value, tool_name))
        return False


# ── HookMatcher (pipe-list, wildcard, regex) ──────────────────────────────

@dataclass(frozen=True, slots=True)
class HookMatcher:
    """Compiled hook matcher — one or more patterns."""

    pattern: str = "*"

    def __post_init__(self) -> None:
        _validate_pipe_pattern(self.pattern)

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


# ── HookSelector ──────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class HookSelector:
    """Composite selector — OR of multiple matchers and/or regex patterns.

    An empty selector matches ALL tools.
    """

    matchers: tuple[HookMatcher, ...] = ()
    regex_matchers: tuple[MatcherPattern, ...] = ()

    @classmethod
    def all_tools(cls) -> "HookSelector":
        return cls()

    @classmethod
    def matching(cls, *patterns: str) -> "HookSelector":
        return cls(matchers=tuple(HookMatcher(p) for p in patterns))

    @classmethod
    def regex(cls, *patterns: str) -> "HookSelector":
        """Create selector with safe compiled regex patterns."""
        validated: list[MatcherPattern] = []
        for p in patterns:
            _validate_regex_safety(p)
            try:
                _re.compile(p)
            except _re.error as e:
                raise MatcherCompileError(
                    f"Invalid regex pattern '{p}': {e}"
                )
            validated.append(MatcherPattern(kind=PatternKind.REGEX, value=p))
        return cls(regex_matchers=tuple(validated))

    def selects(self, tool_name: str) -> bool:
        if not self.matchers and not self.regex_matchers:
            return True  # all_tools()
        for m in self.matchers:
            if m.matches(tool_name):
                return True
        for r in self.regex_matchers:
            if r.matches(tool_name):
                return True
        return False

    def __bool__(self) -> bool:
        return bool(self.matchers) or bool(self.regex_matchers)


# ── Validation ─────────────────────────────────────────────────────────────

def _validate_pipe_pattern(pattern: str) -> None:
    """Reject patterns with regex metacharacters (pipe-list only)."""
    if not pattern or pattern == "*":
        return
    for part in pattern.split("|"):
        part = part.strip()
        if not part or part == "*":
            continue
        # Allow trailing * as prefix wildcard
        candidate = part[:-1] if part.endswith("*") else part
        for ch in ".^$+?{}[]()\\":
            if ch in candidate:
                raise MatcherCompileError(
                    f"Pattern '{part}' contains regex metacharacter '{ch}'. "
                    f"Use exact names, pipe-lists, prefix wildcards, "
                    f"or HookSelector.regex() for explicit regex."
                )


def _validate_regex_safety(pattern: str) -> None:
    """Reject regex patterns with known catastrophic backtracking risks."""
    dangerous = [r"(.+)+", r"(.+)*", r"(.*)+", r"(.*)*",
                 r"(.+){", r"(.*){"]
    for d in dangerous:
        if d in pattern:
            raise MatcherCompileError(
                f"Pattern '{pattern}' contains potentially dangerous "
                f"construct '{d}'.  Use bounded quantifiers instead."
            )
