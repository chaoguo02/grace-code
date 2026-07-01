"""
hitl/settings_loader.py

Load permission rules from .forge-agent/settings.json.
Falls back to builtin defaults when the file doesn't exist.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hitl.permission_rule import PermissionRule


DEFAULT_SETTINGS_FILE = ".forge-agent/settings.json"


def load_permission_settings(
    project_path: str,
    settings_file: str = DEFAULT_SETTINGS_FILE,
) -> tuple[list[PermissionRule], list[dict[str, Any]]]:
    """
    Load permissions and hooks from settings.json.
    Returns (rules, hook_configs).
    Falls back to builtin defaults if file doesn't exist.
    """
    path = Path(project_path) / settings_file
    if not path.exists():
        return _builtin_defaults(), []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _builtin_defaults(), []

    perms = data.get("permissions", {})
    rules: list[PermissionRule] = []

    for raw in perms.get("deny", []):
        try:
            rules.append(PermissionRule.parse(raw, tier="deny", source="settings"))
        except ValueError:
            continue
    for raw in perms.get("ask", []):
        try:
            rules.append(PermissionRule.parse(raw, tier="ask", source="settings"))
        except ValueError:
            continue
    for raw in perms.get("allow", []):
        try:
            rules.append(PermissionRule.parse(raw, tier="allow", source="settings"))
        except ValueError:
            continue

    hooks = data.get("hooks", {}).get("PreToolUse", [])
    return rules, hooks


def save_rule_to_settings(settings_path: str, rule: PermissionRule) -> None:
    """Append a rule to the allow list in settings.json (for 'Always Allow')."""
    path = Path(settings_path)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}

    perms = data.setdefault("permissions", {})
    allow_list = perms.setdefault("allow", [])

    if rule.raw not in allow_list:
        allow_list.append(rule.raw)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _builtin_defaults() -> list[PermissionRule]:
    """
    Sensible defaults when no settings.json exists.
    Equivalent to "acceptEdits" mode: reads auto-allow, writes ask, destructive deny.

    Derives from shell_tool.py's _READONLY_PREFIXES / _CONFIRM_KEYWORDS / _BLOCKED_PATTERNS
    as the single source of truth for command classification.
    """
    from tools.shell_tool import _BLOCKED_PATTERNS, _READONLY_PREFIXES, _CONFIRM_KEYWORDS

    rules: list[PermissionRule] = []

    # ── deny: derived from _BLOCKED_PATTERNS (absolute safety floor) ──
    # Note: Layer 1 (_check_blocked) is the primary defense for these.
    # Layer 3 deny rules are defense-in-depth with prefix matching.
    for pattern in _BLOCKED_PATTERNS:
        safe_pattern = pattern.rstrip()
        # For patterns that end with space or special chars (like "dd if="),
        # use prefix match to catch any suffix
        rules.append(PermissionRule.parse(f"shell({safe_pattern} *)", tier="deny", source="builtin"))

    # ── allow: read-only tools (non-shell) ──
    allow_tools = [
        "file_read",
        "file_view",
        "search_text",
        "find_files",
        "find_symbol",
        "git_status",
        "git_diff",
        "web_search",
        "web_fetch",
    ]
    for t in allow_tools:
        rules.append(PermissionRule.parse(t, tier="allow", source="builtin"))

    # ── allow: derived from _READONLY_PREFIXES ──
    for prefix in _READONLY_PREFIXES:
        # Trailing * = prefix match: matches "ls" alone and "ls -la" etc.
        rules.append(PermissionRule.parse(f"shell({prefix} *)", tier="allow", source="builtin"))

    # ── ask: file write operations ──
    rules.append(PermissionRule.parse("file_write", tier="ask", source="builtin"))
    rules.append(PermissionRule.parse("file_edit", tier="ask", source="builtin"))

    # ── ask: derived from _CONFIRM_KEYWORDS ──
    for keyword in _CONFIRM_KEYWORDS:
        keyword = keyword.rstrip()
        if not keyword:
            continue
        # Special cases: "> " and "| tee " are output redirection patterns
        if keyword.startswith(">") or keyword.startswith("|"):
            continue
        # Trailing * = prefix match: "git push *" matches "git push" and "git push origin"
        rules.append(PermissionRule.parse(f"shell({keyword} *)", tier="ask", source="builtin"))

    return rules
