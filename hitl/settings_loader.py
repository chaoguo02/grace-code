"""
hitl/settings_loader.py

Load permission rules from .grace/settings.json.
Falls back to builtin defaults when the file doesn't exist.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hitl.permission_rule import PermissionRule, PermissionRuleTier


DEFAULT_SETTINGS_FILE = ".forge-agent/settings.json"


def builtin_native_rules() -> list[PermissionRule]:
    """CC-aligned native defaults (headless, no interactive prompt).

    Difference from ``_builtin_defaults()``: Write/Edit are NOT marked ASK.
    CC's coding agent runs in acceptEdits mode — file edits are auto-approved,
    only dangerous operations (destructive shell, network) require interaction.
    In native headless, ask = auto-deny, so marking Write/Edit as ASK would
    block ALL file writes.  The native object graph sets permission_mode=
    acceptEdits and uses this rule set.

    Layering (CC-aligned):
      - deny:      _BLOCKED_PATTERNS (Layer 1 safety floor) + destructive shell
      - allow:     read-only tools + read-only shell commands
      - ask:       destructive / network commands (docker, rm, git push, curl...)
      - Write/Edit: NOT in any rule → acceptEdits mode auto-approves them
    """
    from tools.shell_tool import _BLOCKED_PATTERNS

    rules: list[PermissionRule] = []

    # deny: derived from _BLOCKED_PATTERNS (absolute safety floor)
    for pattern in _BLOCKED_PATTERNS:
        safe_pattern = pattern.rstrip()
        rules.append(PermissionRule.parse(f"shell({safe_pattern} *)", tier=PermissionRuleTier.DENY, source="builtin"))

    # allow: read-only tools (non-shell)
    allow_tools = [
        "Read", "file_view", "Grep", "Glob", "find_symbol",
        "WebSearch", "WebFetch", "git_status", "git_diff",
    ]
    for t in allow_tools:
        rules.append(PermissionRule.parse(t, tier=PermissionRuleTier.ALLOW, source="builtin"))

    # allow: read-only shell commands
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

    # ask: potentially destructive or network-exposed commands (headless → auto-deny)
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


def build_native_hook_settings(
    project_path: str,
    settings_file: str = DEFAULT_SETTINGS_FILE,
) -> dict[str, Any]:
    """Convert project settings.json into the native ``hook_settings`` dict.

    The native object graph (``composition.runtime_composition.assemble``)
    consumes a ``hook_settings`` dict shaped like::

        {
            "hooks": {...},
            "permission_rules": {"Write": "deny", "Read": "allow"},
            "permission_mode": "acceptEdits",
        }

    Legacy settings.json stores permissions as flat arrays under
    ``permissions.deny/ask/allow``.  This helper reads that file and reshapes
    it into the native format so web/CLI entry points can wire real rules into
    the PermissionPipeline instead of falling back to builtin defaults.
    """
    # Project settings live at .grace/settings.json; legacy bootstrap read
    # .forge-agent/settings.json.  Prefer the project location, fall back to
    # the legacy one so both shapes are honored.
    candidates = [
        Path(project_path) / settings_file,
        Path(project_path) / ".grace" / "settings.json",
    ]
    data: dict[str, Any] = {}
    for _candidate in candidates:
        if _candidate.exists():
            try:
                data = json.loads(_candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
            break

    permission_rules: dict[str, str] = {}
    perms = data.get("permissions", {})
    for tier in ("deny", "ask", "allow"):
        for raw in perms.get(tier, []):
            permission_rules[str(raw)] = tier

    return {
        "hooks": data.get("hooks", {}),
        "permission_rules": permission_rules,
        "permission_mode": str(data.get("permission_mode", "") or ""),
    }


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
