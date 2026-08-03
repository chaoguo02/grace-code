"""
G13: Process Runner — argv-based, shell=False, byte caps, timeout→kill.

- HookCommand: frozen argv tuple (never a shell string).
- stdin: canonical JSON of the hook input.
- stdout/stderr byte caps: MAX_STDOUT_BYTES / MAX_STDERR_BYTES.
- timeout: terminate → grace period → kill → reap (no orphans).
- ProcessRegistry: track running processes for cancellation.
- Exit code 0 → parse JSON decision.  2 → blocking error.
  Other → non-blocking error (fail-open/closed per policy).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time as _time
from dataclasses import dataclass, field

from core.json_values import FrozenJsonObject, thaw_json

MAX_STDOUT_BYTES = 64 * 1024   # 64 KiB
MAX_STDERR_BYTES = 64 * 1024
GRACE_PERIOD_S = 2.0
KILL_TIMEOUT_S = 5.0


# ── HookCommand ────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class HookCommand:
    """Immutable hook command — argv tuple, never a shell string."""
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()  # extra env vars
    cwd: str = ""

    def __post_init__(self) -> None:
        if not self.argv or not self.argv[0].strip():
            raise ValueError("HookCommand.argv must not be empty")


# ── Process result ─────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    killed: bool = False
    duration_ms: float = 0.0


# ── ProcessRegistry ────────────────────────────────────────────────────────

class ProcessRegistry:
    """Tracks running subprocesses for cancellation."""

    def __init__(self) -> None:
        self._processes: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def register(self, hook_name: str, proc: subprocess.Popen) -> None:
        with self._lock:
            self._processes[hook_name] = proc

    def unregister(self, hook_name: str) -> None:
        with self._lock:
            self._processes.pop(hook_name, None)

    def cancel(self, hook_name: str) -> bool:
        """Kill a running hook process.  Returns True if found."""
        with self._lock:
            proc = self._processes.pop(hook_name, None)
        if proc is None:
            return False
        return _kill_process(proc)

    def cancel_all(self) -> int:
        """Kill all tracked processes.  Returns count."""
        with self._lock:
            procs = list(self._processes.values())
            self._processes.clear()
        for proc in procs:
            _kill_process(proc)
        return len(procs)


# ── ProcessRunner ──────────────────────────────────────────────────────────

class ProcessRunner:
    """Runs a HookCommand in a subprocess with timeout and byte caps.

    Timeout protocol: terminate → grace → kill → reap.
    No shell=True.  stdin is canonical JSON.
    """

    def __init__(self, registry: ProcessRegistry | None = None) -> None:
        self._registry = registry or ProcessRegistry()

    def run(
        self,
        hook_name: str,
        command: HookCommand,
        hook_input: object,
        timeout_s: float = 30.0,
    ) -> ProcessResult:
        """Execute *command* with *hook_input* on stdin."""
        # Serialize input
        if isinstance(hook_input, FrozenJsonObject):
            input_json = json.dumps(thaw_json(hook_input), sort_keys=True)
        elif hasattr(hook_input, '__dataclass_fields__'):
            from dataclasses import asdict
            input_json = json.dumps(asdict(hook_input), sort_keys=True, default=str)  # type: ignore[call-overload]
        else:
            input_json = json.dumps(hook_input, default=str)

        started = _time.monotonic()

        try:
            proc = subprocess.Popen(
                list(command.argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,  # G13: never shell=True
                cwd=command.cwd or None,
                env={**os.environ, **dict(command.env)} if command.env else None,
            )
        except OSError as exc:
            return ProcessResult(
                returncode=-1, stdout="", stderr=str(exc),
                duration_ms=(_time.monotonic() - started) * 1000,
            )

        self._registry.register(hook_name, proc)

        try:
            try:
                stdout_data, stderr_data = proc.communicate(
                    input=input_json, timeout=timeout_s,
                )
            except subprocess.TimeoutExpired:
                # G13: terminate → grace → kill → reap
                proc.terminate()
                try:
                    stdout_data, stderr_data = proc.communicate(
                        timeout=GRACE_PERIOD_S,
                    )
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout_data, stderr_data = proc.communicate(
                        timeout=KILL_TIMEOUT_S,
                    )
                return ProcessResult(
                    returncode=proc.returncode if proc.returncode is not None else -1,
                    stdout=_truncate(stdout_data or "", MAX_STDOUT_BYTES),
                    stderr=_truncate(stderr_data or "", MAX_STDERR_BYTES),
                    timed_out=True,
                    killed=True,
                    duration_ms=(_time.monotonic() - started) * 1000,
                )
        finally:
            self._registry.unregister(hook_name)

        return ProcessResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=_truncate(stdout_data or "", MAX_STDOUT_BYTES),
            stderr=_truncate(stderr_data or "", MAX_STDERR_BYTES),
            duration_ms=(_time.monotonic() - started) * 1000,
        )


# ── Helpers ─────────────────────────────────────────────────────────────────

def _truncate(data: str, max_bytes: int) -> str:
    encoded = data.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return data
    return encoded[:max_bytes].decode("utf-8", errors="replace") + "\n[...truncated]"


def _kill_process(proc: subprocess.Popen) -> bool:
    """Kill a process.  Returns True if successful."""
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=GRACE_PERIOD_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=KILL_TIMEOUT_S)
        return True
    except Exception:
        return False
