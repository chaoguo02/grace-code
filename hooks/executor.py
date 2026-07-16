"""
hooks/executor.py

Hook executor: runs external command hooks through Runtime.

Communication protocol:
- stdin: JSON context (HookContext.to_dict())
- stdout: optional JSON (HookOutput) or plain text
- stderr: error/reason text (used as block reason on exit 2)
- exit code: 0=success, 2=block, other=non-blocking error
"""

from __future__ import annotations

import json
import logging

from hooks.events import HookContext
from hooks.protocol import HookOutput, HookResult
from tools.runtime import LocalRuntime, ProcessTermination, Runtime

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60


def execute_hook(
    command: str,
    context: HookContext,
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str | None = None,
    runtime: Runtime | None = None,
) -> HookResult:
    """
    Execute a command-type hook through the injected Runtime.

    The context is passed as JSON on stdin.
    stdout is parsed as JSON (HookOutput) if possible, else treated as plain text.
    """
    stdin_data = json.dumps(context.to_dict(), ensure_ascii=False)

    try:
        if runtime is None:
            from pathlib import Path

            root = Path(cwd or Path.cwd()).resolve()
            runtime = LocalRuntime(workspace_root=root)
        result = runtime.exec(
            command,
            cwd=cwd,
            timeout=timeout,
            stdin_data=stdin_data,
        )
    except Exception as exc:
        logger.warning("Hook execution failed: %s — %s", command, exc)
        return HookResult(exit_code=1, stderr=str(exc))

    if result.termination is ProcessTermination.TIMED_OUT:
        logger.warning("Hook timed out after %ds: %s", timeout, command)
        return HookResult(exit_code=1, stderr=f"Hook timed out after {timeout}s")

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    parsed = _try_parse_output(stdout)

    return HookResult(
        exit_code=result.returncode,
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
