"""
P10: Hook matcher — compiled patterns.  Compile failure → reject config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class MatcherCompileError(ValueError):
    """Pattern could not be compiled — config must be rejected."""


@dataclass(frozen=True, slots=True)
class HookMatcher:
    """Matches hook invocations by tool name pattern."""

    pattern: str
    _compiled: re.Pattern = None  # set in __post_init__

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "_compiled", re.compile(self.pattern))
        except re.error as exc:
            raise MatcherCompileError(
                f"Invalid hook matcher pattern '{self.pattern}': {exc}"
            ) from exc

    def matches(self, tool_name: str) -> bool:
        return bool(self._compiled.match(tool_name))


@dataclass(frozen=True, slots=True)
class HookSelector:
    """Selects which hooks fire for an event.

    If matchers is empty, the hook fires for ALL tools.
    If matchers is non-empty, the hook fires only for matching tools.
    """

    matchers: tuple[HookMatcher, ...] = ()

    @classmethod
    def all_tools(cls) -> HookSelector:
        return cls()

    @classmethod
    def matching(cls, *patterns: str) -> HookSelector:
        return cls(matchers=tuple(HookMatcher(p) for p in patterns))

    def selects(self, tool_name: str) -> bool:
        if not self.matchers:
            return True
        return any(m.matches(tool_name) for m in self.matchers)
