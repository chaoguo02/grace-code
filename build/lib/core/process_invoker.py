"""ProcessInvoker — lightweight synchronous process adapter.

CC-aligned: shared UTF-8 decode, timeout, cwd safety, and error
classification for BOTH user-tool execution and internal fact probes.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from core.utf8 import DEFAULT_TEXT_ENCODING, DEFAULT_TEXT_ERRORS, with_utf8_env


@dataclass
class InvokeResult:
    returncode: int
    stdout: str
    stderr: str
    success: bool
    timed_out: bool = False
    start_failed: bool = False


class ProcessInvoker:
    """Lightweight process runner with uniform safety guarantees.

    Used by: ShellTool, PytestTool, project_environment, workspace_facts.
    NOT a replacement for LocalRuntime — Runtime manages process trees and
    cancellation. ProcessInvoker is for simple fire-and-wait calls.
    """

    DEFAULT_TIMEOUT = 30
    DEFAULT_ENCODING = DEFAULT_TEXT_ENCODING

    def __init__(self, workspace_root: str | Path):
        self._workspace_root = Path(workspace_root).resolve()

    def run(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        env: dict[str, str] | None = None,
    ) -> InvokeResult:
        """Execute a command with parameterized args (shell=False).

        Args:
            cmd: Command + args as list (e.g. ["git", "status"])
            cwd: Working directory (validated against workspace_root)
            timeout: Seconds before SIGKILL
            env: Extra env vars (merged with os.environ)
        """
        resolved_cwd = self._resolve_cwd(cwd)
        proc: subprocess.Popen | None = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(resolved_cwd),
                env=with_utf8_env(env),
                text=False,
            )
            stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
            return InvokeResult(
                returncode=proc.returncode or 0,
                stdout=self._decode(stdout_bytes or b""),
                stderr=self._decode(stderr_bytes or b""),
                success=proc.returncode == 0,
            )
        except subprocess.TimeoutExpired:
            if proc:
                proc.kill()
                proc.wait(timeout=5)
            return InvokeResult(
                returncode=-1, stdout="", stderr=f"Timeout after {timeout}s",
                success=False, timed_out=True,
            )
        except (OSError, FileNotFoundError) as exc:
            return InvokeResult(
                returncode=-1, stdout="", stderr=str(exc),
                success=False, start_failed=True,
            )

    def _resolve_cwd(self, cwd: str | None) -> Path:
        target = Path(cwd or self._workspace_root).resolve()
        try:
            target.relative_to(self._workspace_root)
        except ValueError:
            raise ValueError(
                f"Process cwd {target} is outside workspace {self._workspace_root}"
            ) from None
        if not target.is_dir():
            raise ValueError(f"Process cwd does not exist: {target}")
        return target

    @staticmethod
    def _decode(data: bytes) -> str:
        try:
            return data.decode(DEFAULT_TEXT_ENCODING)
        except UnicodeDecodeError:
            return data.decode(DEFAULT_TEXT_ENCODING, errors=DEFAULT_TEXT_ERRORS)
