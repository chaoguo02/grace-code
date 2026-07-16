"""
hitl/permission_rule.py

Permission rule: Tool(pattern) syntax parsing and glob matching.

Rule format:
  "shell(git commit *)"   — prefix match: "git commit" + any trailing content (incl. empty)
  "shell(git push)"       — exact match for bare "git push"
  "file_edit(./src/*)"    — prefix match under src/
  "file_read"             — matches all file_read calls (no pattern = wildcard)

Wildcard semantics (aligned with Claude Code):
  Trailing " *" → prefix match: matches the prefix alone OR prefix + any content
    e.g. "git push *" matches both "git push" and "git push origin main"
  Middle *     → matches one non-space token [^ ]*
    e.g. "git * --all" matches "git fetch --all" but not "git fetch origin --all"
  No *         → exact match
  No ** syntax — Claude Code only uses single *
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


_RULE_RE = re.compile(r"^(\w+)(?:\((.+)\))?$")


class PermissionRuleTier(str, Enum):
    DENY = "deny"
    ASK = "ask"
    ALLOW = "allow"


@dataclass(frozen=True)
class PermissionRule:
    raw: str
    tool_name: str
    pattern: str | None
    tier: PermissionRuleTier
    source: str = "settings"

    def __post_init__(self) -> None:
        if not isinstance(self.tier, PermissionRuleTier):
            object.__setattr__(self, "tier", PermissionRuleTier(self.tier))

    @classmethod
    def parse(
        cls,
        raw: str,
        tier: PermissionRuleTier | str,
        source: str = "settings",
    ) -> "PermissionRule":
        raw = raw.strip()
        m = _RULE_RE.match(raw)
        if not m:
            raise ValueError(f"Invalid rule syntax: {raw!r}")
        tool_name = m.group(1).lower()
        pattern = m.group(2)
        return cls(
            raw=raw,
            tool_name=tool_name,
            pattern=pattern,
            tier=PermissionRuleTier(tier),
            source=source,
        )

    def matches(self, tool_name: str, params: dict[str, Any]) -> bool:
        if self.tool_name != tool_name.lower() and self.tool_name != "*":
            return False
        if self.pattern is None:
            return True
        target = _extract_match_target(tool_name, params)
        return _glob_match(self.pattern, target)


def _extract_match_target(tool_name: str, params: dict[str, Any]) -> str:
    name = tool_name.lower()
    if name == "shell":
        return params.get("cmd", "")
    if name in ("file_write", "file_edit", "file_read", "file_view", "read", "write", "edit"):
        return params.get("path", "") or params.get("file_path", "")
    if name in ("git_add", "git_commit"):
        return params.get("message", "") or params.get("path", "") or ""
    if name in ("search_text", "find_files", "find_symbol", "grep", "glob"):
        return params.get("path", "") or params.get("pattern", "")
    if name in ("agent", "task"):
        return params.get("subagent_type", "") or params.get("agent_name", "")
    return " ".join(str(v) for v in params.values())


def _glob_match(pattern: str, target: str) -> bool:
    regex = _pattern_to_regex(pattern)
    return bool(re.match(regex, target, re.IGNORECASE))


def _pattern_to_regex(pattern: str) -> str:
    """Convert a Tool(pattern) glob to a regex.

    Trailing " *" → prefix match (matches prefix alone or prefix + anything).
    Middle *      → matches one non-space token [^ ]*.
    No *          → exact match.
    """
    if pattern.endswith(" *"):
        # Trailing * = prefix match: the prefix itself, or prefix + whitespace + anything
        prefix = pattern[:-2]
        escaped = re.escape(prefix)
        return f"^{escaped}(\\s.*)?$"
    elif "*" in pattern:
        # Middle * = one non-space token
        escaped = re.escape(pattern).replace(r"\*", "[^ ]*")
        return f"^{escaped}$"
    else:
        # No wildcard = exact match
        return f"^{re.escape(pattern)}$"
