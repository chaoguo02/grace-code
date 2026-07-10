"""
hooks/matcher.py

Hook matcher: filters which hooks fire for a given tool call.

Matcher syntax (aligned with Claude Code):
- "*"                   → match all tools
- "shell"              → exact match on tool name
- "file_write|file_edit" → alternation (pipe-separated)
- if_condition: "tool_input.cmd matches 'git push*'" → field-level glob filter
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class HookMatcher:
    pattern: str = "*"
    if_condition: str | None = None

    def matches(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        if not self._match_tool_name(tool_name):
            return False
        if self.if_condition:
            return self._evaluate_if(tool_input)
        return True

    def _match_tool_name(self, tool_name: str) -> bool:
        if self.pattern == "*":
            return True
        # Pipe-separated alternation: "file_write|file_edit"
        alternatives = [p.strip() for p in self.pattern.split("|")]
        return tool_name in alternatives

    def _evaluate_if(self, tool_input: dict[str, Any]) -> bool:
        """
        Evaluate an if-condition against tool_input.
        Supports: "tool_input.FIELD matches 'PATTERN'"
        where PATTERN uses * as glob (matches any non-whitespace).
        """
        m = _IF_RE.match(self.if_condition or "")
        if not m:
            return True
        field = m.group(1)
        glob_pattern = m.group(2)
        value = str(tool_input.get(field, ""))
        regex = re.escape(glob_pattern).replace(r"\*", ".*")
        return bool(re.match(f"^{regex}$", value, re.IGNORECASE))


_IF_RE = re.compile(
    r"tool_input\.(\w+)\s+matches\s+'([^']*)'",
    re.IGNORECASE,
)
