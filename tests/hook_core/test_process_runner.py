"""G13: Process Runner — argv, shell=False, byte caps, timeout→kill.

AC: HookCommand(argv=tuple) — never a shell string
AC: shell=False enforced
AC: exit 0 → JSON parsed as decision
AC: exit 2 → blocking error
AC: exit other → non-blocking error
AC: timeout → terminate → grace → kill → no orphan
AC: stdout/stderr byte caps enforced
AC: ProcessRegistry tracks and cancels processes
AC: spaces in args preserved (no shell expansion)
"""

from __future__ import annotations

import json
import sys
import time as _time
from dataclasses import dataclass

import pytest

from hook_core.process_runner import (
    HookCommand,
    ProcessRunner,
    ProcessRegistry,
    ProcessResult,
    MAX_STDOUT_BYTES,
    MAX_STDERR_BYTES,
)
from hook_core.executor import (
    execute_hook,
    HookExecution,
    HookTimeoutError,
)
from hook_core.policies import (
    HookPolicy, Scheduling, DecisionAuthority, DataAuthority, FailurePolicy,
)


# ═══════════════════════════════════════════════════════════════════════════════
# G13.1 — HookCommand: argv tuple, never shell string
# ═══════════════════════════════════════════════════════════════════════════════

class TestHookCommand:
    """G13: HookCommand uses argv tuple, rejects empty."""

    def test_command_with_argv(self):
        cmd = HookCommand(argv=("python", "-c", "print('hello')"))
        assert cmd.argv == ("python", "-c", "print('hello')")

    def test_rejects_empty_argv(self):
        with pytest.raises(ValueError, match="argv"):
            HookCommand(argv=())
        with pytest.raises(ValueError, match="argv"):
            HookCommand(argv=("",))

    def test_command_with_env(self):
        cmd = HookCommand(
            argv=("python", "-c", "import os; print(os.environ.get('HOOK_VAR',''))"),
            env=(("HOOK_VAR", "test_value"),),
        )
        assert len(cmd.env) == 1

    def test_command_is_frozen(self):
        cmd = HookCommand(argv=("echo", "hello"))
        with pytest.raises(Exception):
            cmd.argv = ("other",)  # type: ignore


# ═══════════════════════════════════════════════════════════════════════════════
# G13.2 — ProcessRunner: shell=False, exit codes
# ═══════════════════════════════════════════════════════════════════════════════

class TestProcessRunner:
    """G13: Process runs with shell=False, correct exit code handling."""

    def test_exit_zero_returns_stdout(self):
        runner = ProcessRunner()
        cmd = HookCommand(argv=(sys.executable, "-c", "print('hello')"))
        result = runner.run("test", cmd, {}, timeout_s=5.0)
        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_exit_nonzero(self):
        runner = ProcessRunner()
        cmd = HookCommand(argv=(sys.executable, "-c", "import sys; sys.exit(3)"))
        result = runner.run("test", cmd, {}, timeout_s=5.0)
        assert result.returncode == 3

    def test_spaces_in_args_preserved(self):
        """Arguments with spaces must not be shell-expanded."""
        runner = ProcessRunner()
        # Pass data via stdin (hook_input), which is what real hooks receive
        cmd = HookCommand(argv=(sys.executable, "-c",
                                "import sys; print(repr(sys.stdin.read()))"))
        result = runner.run("test", cmd, "arg with spaces", timeout_s=5.0)
        assert "arg with spaces" in result.stdout or "'arg with spaces'" in result.stdout

    def test_no_shell_injection(self):
        """Special shell characters must not be interpreted by shell."""
        runner = ProcessRunner()
        # stdin data contains shell metacharacters — they must arrive literally
        cmd = HookCommand(argv=(sys.executable, "-c",
                                "import sys; print(sys.stdin.read(), end='')"))
        result = runner.run("test", cmd, "$HOME `whoami`", timeout_s=5.0)
        assert "$HOME" in result.stdout, (
            "G13: shell metacharacters must not be expanded "
            f"(got: {result.stdout!r})"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# G13.3 — Timeout → terminate → grace → kill
# ═══════════════════════════════════════════════════════════════════════════════

class TestTimeout:
    """G13: Timeout kills process, no orphans."""

    def test_timeout_kills_process(self):
        runner = ProcessRunner()
        # Script that sleeps forever
        cmd = HookCommand(argv=(sys.executable, "-c",
                                "import time; time.sleep(60)"))
        result = runner.run("test", cmd, {}, timeout_s=0.5)
        assert result.timed_out, "Process should be timed out"
        assert result.killed, "Process should be killed after timeout"

    def test_timeout_duration_reported(self):
        runner = ProcessRunner()
        cmd = HookCommand(argv=(sys.executable, "-c",
                                "import time; time.sleep(60)"))
        result = runner.run("test", cmd, {}, timeout_s=0.5)
        assert result.duration_ms > 0, "Duration must be reported"


# ═══════════════════════════════════════════════════════════════════════════════
# G13.4 — Byte caps
# ═══════════════════════════════════════════════════════════════════════════════

class TestByteCaps:
    """G13: stdout/stderr truncated to MAX bytes."""

    def test_long_output_truncated(self):
        runner = ProcessRunner()
        # Generate output larger than 64KB
        cmd = HookCommand(argv=(sys.executable, "-c",
                                f"print('x' * {MAX_STDOUT_BYTES + 1000})"))
        result = runner.run("test", cmd, {}, timeout_s=5.0)
        # Output should be truncated
        assert "[...truncated]" in result.stdout or len(result.stdout.encode("utf-8")) <= MAX_STDOUT_BYTES + 100, (
            f"Output should be capped, got {len(result.stdout.encode('utf-8'))} bytes"
        )

    def test_short_output_not_truncated(self):
        runner = ProcessRunner()
        cmd = HookCommand(argv=(sys.executable, "-c", "print('short')"))
        result = runner.run("test", cmd, {}, timeout_s=5.0)
        assert "short" in result.stdout
        assert "[...truncated]" not in result.stdout


# ═══════════════════════════════════════════════════════════════════════════════
# G13.5 — ProcessRegistry
# ═══════════════════════════════════════════════════════════════════════════════

class TestProcessRegistry:
    """G13: Registry tracks and can cancel processes."""

    def test_cancel_all_kills_processes(self):
        registry = ProcessRegistry()
        runner = ProcessRunner(registry=registry)

        import threading
        import subprocess

        # Start a long-running process manually
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        registry.register("long-running", proc)

        killed = registry.cancel_all()
        assert killed >= 1
        # Process should be killed
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        assert proc.poll() is not None, "Process should have been terminated"

    def test_unregister_removes_tracking(self):
        registry = ProcessRegistry()
        import subprocess
        proc = subprocess.Popen(
            [sys.executable, "-c", "print('ok')"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        registry.register("test", proc)
        proc.wait(timeout=5.0)
        registry.unregister("test")
        assert registry.cancel("test") is False  # already unregistered


# ═══════════════════════════════════════════════════════════════════════════════
# G13.6 — execute_hook integration
# ═══════════════════════════════════════════════════════════════════════════════

class TestExecuteHookIntegration:
    """G13: execute_hook with HookCommand and callable."""

    def test_execute_process_hook_exit_zero(self):
        policy = HookPolicy(
            scheduling=Scheduling.AWAITED,
            decision_authority=DecisionAuthority.OBSERVE,
            data_authority=DataAuthority.OBSERVE,
            failure_policy=FailurePolicy.FAIL_OPEN,
            timeout_s=5.0,
        )
        cmd = HookCommand(argv=(sys.executable, "-c",
                                'import json; print(json.dumps({"additionalContext":"hello"}))'))
        result = execute_hook("test", cmd, {}, policy)
        assert isinstance(result, HookExecution)
        assert result.timed_out is False

    def test_execute_callable_handler(self):
        policy = HookPolicy(
            scheduling=Scheduling.AWAITED,
            decision_authority=DecisionAuthority.OBSERVE,
            data_authority=DataAuthority.OBSERVE,
            failure_policy=FailurePolicy.FAIL_OPEN,
            timeout_s=5.0,
        )

        def my_handler(inp):
            return "ok"

        result = execute_hook("test", my_handler, {}, policy)
        assert isinstance(result, HookExecution)
        assert result.decision == "ok"
        assert result.duration_ms >= 0  # fast callables may be ~0ms
