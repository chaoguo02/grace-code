"""
hitl/settings_loader.py

Load permission rules from .forge-agent/settings.json.
Falls back to builtin defaults when the file doesn't exist.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hitl.permission_rule import PermissionRule, PermissionRuleTier


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
            rules.append(PermissionRule.parse(raw, tier=PermissionRuleTier.DENY, source="settings"))
        except ValueError:
            continue
    for raw in perms.get("ask", []):
        try:
            rules.append(PermissionRule.parse(raw, tier=PermissionRuleTier.ASK, source="settings"))
        except ValueError:
            continue
    for raw in perms.get("allow", []):
        try:
            rules.append(PermissionRule.parse(raw, tier=PermissionRuleTier.ALLOW, source="settings"))
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

    Command classification is now declarative — the constants _READONLY_PREFIXES
    and _CONFIRM_KEYWORDS were removed from shell_tool.py in favor of
    PhasePolicy.allowed_effects. The allow/ask lists below are inline defaults
    that match Claude Code's acceptEdits permission model.
    """
    from tools.shell_tool import _BLOCKED_PATTERNS

    rules: list[PermissionRule] = []

    # ── deny: derived from _BLOCKED_PATTERNS (absolute safety floor) ──
    # Note: Layer 1 (_check_blocked) is the primary defense for these.
    # Layer 3 deny rules are defense-in-depth with prefix matching.
    for pattern in _BLOCKED_PATTERNS:
        safe_pattern = pattern.rstrip()
        rules.append(PermissionRule.parse(f"shell({safe_pattern} *)", tier=PermissionRuleTier.DENY, source="builtin"))

    # ── allow: read-only tools (non-shell) — aligned with Claude Code ──
    # Must match canonical tool names (not aliases) — the permission pipeline
    # checks tool.name, and aliases are only resolved at execute_tool() time.
    allow_tools = [
        "Read",         # was "file_read"
        "file_view",    # unchanged
        "Grep",         # was "search_text"
        "Glob",         # was "find_files"
        "find_symbol",  # unchanged
        "WebSearch",    # unchanged (already PascalCase)
        "WebFetch",     # unchanged (already PascalCase)
        "git_status",   # read-only git inspection
        "git_diff",     # read-only git diff
    ]
    for t in allow_tools:
        rules.append(PermissionRule.parse(t, tier=PermissionRuleTier.ALLOW, source="builtin"))

    # ── allow: read-only shell commands (safe commands with no side effects) ──
    _READONLY_COMMANDS = (
        "ls", "dir", "pwd", "echo", "cat", "head", "tail",
        "wc", "sort", "uniq", "cut", "tr",
        "date", "env", "printenv", "which", "type",
        "du", "df", "free", "uptime",
        "find", "locate", "xargs", "tee",
        "grep", "rg", "awk", "sed",
    )
    for cmd in _READONLY_COMMANDS:
        rules.append(PermissionRule.parse(f"shell({cmd} *)", tier=PermissionRuleTier.ALLOW, source="builtin"))

    # ── ask: file write operations ──
    rules.append(PermissionRule.parse("Write", tier=PermissionRuleTier.ASK, source="builtin"))
    rules.append(PermissionRule.parse("file_write", tier=PermissionRuleTier.ASK, source="builtin"))
    rules.append(PermissionRule.parse("Edit", tier=PermissionRuleTier.ASK, source="builtin"))
    rules.append(PermissionRule.parse("file_edit", tier=PermissionRuleTier.ASK, source="builtin"))

    # ── ask: potentially destructive or network-exposed commands ──
    _CONFIRM_COMMANDS = (
        "git push", "git commit", "npm publish", "npm install -g",
        "pip install", "docker", "docker-compose", "kubectl", "helm",
        "terraform", "ansible", "systemctl", "service",
        "chmod", "chown", "rm", "mv", "cp -r",
        "scp", "rsync", "curl", "wget",
    )
    for cmd in _CONFIRM_COMMANDS:
        rules.append(PermissionRule.parse(f"shell({cmd} *)", tier=PermissionRuleTier.ASK, source="builtin"))

    return rules
