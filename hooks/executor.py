"""
hooks/executor.py

Hook executor: runs external command hooks via subprocess.

Communication protocol:
- stdin: JSON context (HookContext.to_dict())
- stdout: optional JSON (HookOutput) or plain text
- stderr: error/reason text (used as block reason on exit 2)
- exit code: 0=success, 2=block, other=non-blocking error
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from hooks.events import HookContext
from hooks.protocol import HookOutput, HookResult

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60


def execute_hook(
    command: str,
    context: HookContext,
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str | None = None,
) -> HookResult:
    """
    Execute a command-type hook via subprocess.

    The context is passed as JSON on stdin.
    stdout is parsed as JSON (HookOutput) if possible, else treated as plain text.
    """
    stdin_data = json.dumps(context.to_dict(), ensure_ascii=False)

    try:
        proc = subprocess.run(
            command,
            shell=True,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Hook timed out after %ds: %s", timeout, command)
        return HookResult(exit_code=1, stderr=f"Hook timed out after {timeout}s")
    except Exception as exc:
        logger.warning("Hook execution failed: %s — %s", command, exc)
        return HookResult(exit_code=1, stderr=str(exc))

    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()

    parsed = _try_parse_output(stdout)

    return HookResult(
        exit_code=proc.returncode,
        stdout=stdout,
        stderr=stderr,
        parsed=parsed,
    )


def _try_parse_output(stdout: str) -> HookOutput | None:
    """Attempt to parse stdout as JSON HookOutput."""
    if not stdout:
        return None
    try:
        data = json.loads(stdout)
        if isinstance(data, dict):
            return HookOutput.from_dict(data)
    except (json.JSONDecodeError, TypeError):
        pass
    return None
