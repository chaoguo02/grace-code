"""
hitl/pattern_inference.py

Infer generalized permission patterns from specific tool calls.
Used by the "Always Allow" feature to create reusable rules.

Strategy: conservative — keep first 2 command tokens, wildcard the rest.
Uses single * (non-space token wildcard) aligned with Claude Code syntax.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from hitl.permission_rule import PermissionRule, PermissionRuleTier


def infer_permission_pattern(tool_name: str, params: dict[str, Any]) -> PermissionRule:
    """
    From a specific tool call, infer a generalized allow rule.

    Examples:
      shell(cmd="git commit -m fix")      → shell(git commit *)
      file_edit(path="src/auth.py")        → file_edit(./src/*)
      file_write(path="tests/test_x.py")   → file_write(./tests/*)
      git_commit(message="fix")            → git_commit
    """
    name = tool_name.lower()

    if name == "shell":
        cmd = params.get("cmd", "")
        pattern = _infer_shell_pattern(cmd)
        return PermissionRule.parse(f"shell({pattern})", tier=PermissionRuleTier.ALLOW, source="session")

    if name in ("file_write", "file_edit"):
        path = params.get("path", "")
        dir_pattern = _infer_path_pattern(path)
        return PermissionRule.parse(f"{name}({dir_pattern})", tier=PermissionRuleTier.ALLOW, source="session")

    if name in ("file_read", "file_view"):
        path = params.get("path", "")
        dir_pattern = _infer_path_pattern(path)
        return PermissionRule.parse(f"{name}({dir_pattern})", tier=PermissionRuleTier.ALLOW, source="session")

    # Generic: allow all calls to this tool
    return PermissionRule.parse(name, tier=PermissionRuleTier.ALLOW, source="session")


def _infer_shell_pattern(cmd: str) -> str:
    """
    Infer a shell command pattern using trailing * (prefix match).
    'git commit -m "fix parser"' → 'git commit *'  (prefix: "git commit")
    'python -m pytest tests/'    → 'python -m *'   (prefix: "python -m")
    'ls -la src/'                → 'ls *'          (prefix: "ls")
    """
    tokens = cmd.split()
    if not tokens:
        return "*"
    if len(tokens) == 1:
        return tokens[0] + " *"
    base = " ".join(tokens[:2])
    return f"{base} *"


def _infer_path_pattern(path: str) -> str:
    """
    Infer a path glob pattern.
    'src/components/Button.tsx' → './src/*'
    'tests/test_foo.py'         → './tests/*'
    'README.md'                 → './*'
    """
    # Normalize to forward slashes
    normalized = path.replace("\\", "/")
    # Strip leading ./
    if normalized.startswith("./"):
        normalized = normalized[2:]

    parts = PurePosixPath(normalized).parts
    if len(parts) >= 2:
        return f"./{parts[0]}/*"
    return "./*"
