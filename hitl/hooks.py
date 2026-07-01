"""
hitl/hooks.py

PreToolUse hook runner. Executes user-defined shell scripts before tool calls.

Hook config (from settings.json):
  {"matcher": "shell(**)", "command": ".forge-agent/hooks/pre-shell.sh", "timeout_ms": 5000}

Exit codes:
  0 = approve (skip remaining pipeline layers)
  1 = abstain (continue to next layer)
  2 = deny (block the tool call)

Tool info is passed via environment variables:
  TOOL_NAME, TOOL_PARAMS_JSON, TOOL_CMD (for shell tool)
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any

from hitl.permission_rule import PermissionRule


@dataclass
class HookConfig:
    matcher: str
    command: str
    timeout_ms: int = 5000

    def matches(self, tool_name: str, params: dict[str, Any]) -> bool:
        try:
            rule = PermissionRule.parse(self.matcher, tier="allow")
            return rule.matches(tool_name, params)
        except ValueError:
            return False


@dataclass
class HookResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def approves(self) -> bool:
        return self.exit_code == 0

    @property
    def denies(self) -> bool:
        return self.exit_code == 2

    @property
    def abstains(self) -> bool:
        return self.exit_code not in (0, 2)


def run_hook(
    hook: HookConfig,
    tool_name: str,
    params: dict[str, Any],
    cwd: str | None = None,
) -> HookResult:
    """
    Run a PreToolUse hook script. Returns HookResult.
    Timeout or error → abstain (exit_code=1).
    """
    env = os.environ.copy()
    env["TOOL_NAME"] = tool_name
    env["TOOL_PARAMS_JSON"] = json.dumps(params, ensure_ascii=False)
    if tool_name.lower() == "shell":
        env["TOOL_CMD"] = params.get("cmd", "")

    try:
        result = subprocess.run(
            hook.command,
            shell=True,
            env=env,
            cwd=cwd,
            timeout=hook.timeout_ms / 1000,
            capture_output=True,
            text=True,
        )
        return HookResult(
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    except subprocess.TimeoutExpired:
        return HookResult(exit_code=1, stderr="Hook timed out")
    except Exception as exc:
        return HookResult(exit_code=1, stderr=str(exc))
