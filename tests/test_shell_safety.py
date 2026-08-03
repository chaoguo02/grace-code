"""P0_3 Batch 3: ShellTool safety — argv mode, workspace guard, cancel_token.

AC mappings:
  AC-4.1  Args with ; | $() passed as literals in argv mode, not interpreted
  AC-4.2  use_shell=true requires explicit opt-in
  AC-4.3  workspace root missing → fail closed
  AC-1.7  cancel_token fires → process killed
"""

from __future__ import annotations

import subprocess
import time

import pytest

from core.cancellation import CancellationHandle


# ===========================================================================
# 1. ARGV injection prevention
# ===========================================================================

class TestArgvSafety:
    """AC-4.1: Special characters in args are not shell-interpreted."""

    def test_special_chars_in_args_not_interpreted(self):
        """Args with ; | $() are literals, not shell commands."""
        from core.process import LocalRuntime

        rt = LocalRuntime(workspace_root=".")
        # Pass a semicolon in an arg — if interpreted, would create a file
        result = rt.execute(
            "python", ["-c", "print('safe; not executed')"],
            timeout=5,
        )
        assert result.returncode == 0
        assert "safe; not executed" in result.stdout
        # The semicolon was a literal, not a command separator

    def test_dollar_sign_not_expanded(self):
        """$HOME in args stays as literal text."""
        from core.process import LocalRuntime

        rt = LocalRuntime(workspace_root=".")
        result = rt.execute(
            "python", ["-c", "import sys; print(repr(sys.argv[1]))", "$HOME"],
            timeout=5,
        )
        assert "$HOME" in result.stdout


# ===========================================================================
# 2. Shell mode explicit opt-in
# ===========================================================================

class TestShellExplicitOptIn:
    """AC-4.2: Shell fallback requires use_shell=true."""

    def test_non_existent_exe_without_use_shell_fails(self):
        """Command not found as exe + use_shell=false → error, not shell fallback."""
        from tools.shell_tool import ShellTool
        from core.process import LocalRuntime

        rt = LocalRuntime(workspace_root=".")
        tool = ShellTool(runtime=rt)

        result = tool.execute({
            "command": "nonexistent_command_xyz_123",
            "args": ["--flag"],
            "timeout": 5,
        })
        assert not result.success
        assert "use_shell" in result.error.lower()

    def test_use_shell_true_allows_fallback(self):
        """use_shell=true allows shell execution for non-exe commands."""
        from tools.shell_tool import ShellTool
        from core.process import LocalRuntime

        rt = LocalRuntime(workspace_root=".")
        tool = ShellTool(runtime=rt)

        result = tool.execute({
            "command": "echo",
            "args": ["hello_shell_test"],
            "timeout": 5,
            "use_shell": True,
        })
        # echo may or may not be a direct exe — either way, should succeed
        # because use_shell=true allows the fallback
        assert result.success or "hello_shell_test" in result.output.lower()


# ===========================================================================
# 3. Cancel during shell execution
# ===========================================================================

class TestCancelDuringShell:
    """AC-1.7: cancel_token kills active shell process."""

    def test_cancel_kills_long_running_process(self):
        """Cancelling the token kills the subprocess."""
        from core.process import LocalRuntime
        import threading

        rt = LocalRuntime(workspace_root=".")
        handle = CancellationHandle()

        # Start a long-running process in a background thread
        result_holder: list = []

        def _run():
            r = rt.execute(
                "python", ["-c", "import time; time.sleep(60)"],
                timeout=120,
                cancel_token=handle,
            )
            result_holder.append(r)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        time.sleep(0.3)  # Let the process start

        # Cancel
        handle.cancel("test-cancel")

        t.join(timeout=10)
        assert not t.is_alive(), "Thread should complete after cancel"
        assert len(result_holder) > 0
        # Process should have been killed (returncode != 0 or terminated)
        r = result_holder[0]
        # On cancel, we expect termination (not normal exit 0)
        assert r.returncode != 0 or r.termination is not None


# ===========================================================================
# 4. Workspace guard
# ===========================================================================

class TestWorkspaceGuard:
    """AC-4.3: Shell tools require workspace root."""

    def test_shell_tool_refuses_without_workspace(self):
        """ShellTool with no workspace root returns error."""
        from tools.shell_tool import ShellTool
        from unittest.mock import MagicMock

        # Runtime mock without _workspace_root
        mock_rt = MagicMock()
        # Remove _workspace_root so getattr returns None
        del mock_rt._workspace_root

        tool = ShellTool(runtime=mock_rt)
        result = tool.execute({"command": "echo", "args": ["test"]})
        assert not result.success
        assert "workspace" in result.error.lower()

    def test_docker_runtime_exposes_workspace_root(self):
        """U4: DockerRuntime must expose _workspace_root so ShellTool's
        fail-closed guard sees a valid boundary instead of 'not set'."""
        from core.process import DockerRuntime, CONTAINER_WORKDIR
        from pathlib import Path

        rt = DockerRuntime(repo_path=".")
        assert rt._workspace_root == Path(CONTAINER_WORKDIR)

    def test_shell_tool_with_docker_runtime_passes_workspace_guard(self):
        """U4: ShellTool + DockerRuntime no longer fails the P0_3 workspace guard."""
        from tools.shell_tool import ShellTool
        from core.process import DockerRuntime
        from unittest.mock import MagicMock

        rt = DockerRuntime(repo_path=".")
        rt.execute = MagicMock(return_value=type(
            "R", (), {"returncode": 0, "stdout": "hello", "stderr": "",
                       "success": True, "output": "hello", "error": None})(),
        )
        tool = ShellTool(runtime=rt)
        # The command does not exist as a direct exe, but the guard must pass
        # (proving ws_root is set) before we hit shell fallback.
        result = tool.execute({"command": "echo", "args": ["test"], "timeout": 5})
        # Not the "Workspace root is not set" error — whatever else happens is fine.
        assert "workspace root" not in (result.error or "").lower()
