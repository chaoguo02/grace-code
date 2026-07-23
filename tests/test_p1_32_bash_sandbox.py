"""
P1-32: Bash sandbox — _BLOCKED_PATTERNS expansion + workspace path validation.

Verifies:
  M1: New destructive patterns are caught by _check_blocked()
  M2: _validate_workspace_paths() blocks workspace escapes
  M4: _ROOT_REMOVAL_PATTERNS synced with _BLOCKED_PATTERNS
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# ────────────────────────────────────────────────────────────────────────────
# M1: _BLOCKED_PATTERNS expansion
# ────────────────────────────────────────────────────────────────────────────

class TestBlockedPatterns:
    """Verify _check_blocked() catches the newly added destructive patterns."""

    def test_blocked_find_root_delete(self):
        """find / -delete must be blocked (root recursive deletion bypass)."""
        from tools.shell_tool import _check_blocked
        assert _check_blocked("find / -delete") != ""

    def test_blocked_find_root_exec_rm(self):
        """find / -exec rm must be blocked (root deletion via -exec bypass)."""
        from tools.shell_tool import _check_blocked
        assert _check_blocked("find / -exec rm -rf {} ;") != ""

    def test_blocked_chmod_000_root(self):
        """chmod -R 000 / must be blocked (revoke all permissions)."""
        from tools.shell_tool import _check_blocked
        assert _check_blocked("chmod -R 000 /") != ""

    def test_blocked_dd_with_separate_args(self):
        """dd if=... of=/dev/sda — when passed as command+args, cmd_repr still
        contains 'dd if=' so it must match."""
        from tools.shell_tool import _check_blocked
        # Simulate the cmd_repr that _execute_parameterized builds
        cmd_repr = "dd if=/dev/zero of=/dev/sda"
        assert _check_blocked(cmd_repr) != ""

    def test_blocked_nvme_overwrite(self):
        """> /dev/nvme* must be blocked (NVMe disk overwrite)."""
        from tools.shell_tool import _check_blocked
        assert _check_blocked("cat /tmp/x > /dev/nvme0n1") != ""

    def test_blocked_rm_force_root_glob(self):
        """rm -rf /* must be blocked (root glob deletion)."""
        from tools.shell_tool import _check_blocked
        assert _check_blocked("rm -rf /*") != ""

    def test_legitimate_find_not_blocked(self):
        """find . -name '*.py' must NOT be blocked (normal usage)."""
        from tools.shell_tool import _check_blocked
        assert _check_blocked("find . -name '*.py'") == ""

    def test_legitimate_chmod_not_blocked(self):
        """chmod 755 script.sh must NOT be blocked (normal usage)."""
        from tools.shell_tool import _check_blocked
        assert _check_blocked("chmod 755 script.sh") == ""


# ────────────────────────────────────────────────────────────────────────────
# M2: _validate_workspace_paths()
# ────────────────────────────────────────────────────────────────────────────

class TestWorkspacePathValidation:
    """Verify _validate_workspace_paths() enforces workspace boundary on args."""

    @pytest.fixture
    def ws(self, tmp_path):
        return str(tmp_path.resolve())

    def test_absolute_path_inside_workspace(self, ws):
        """Arg with absolute path inside workspace → None (safe)."""
        from tools.shell_tool import _validate_workspace_paths
        safe_path = str(Path(ws) / "src" / "main.py")
        (Path(ws) / "src").mkdir(parents=True, exist_ok=True)
        (Path(ws) / "src" / "main.py").write_text("pass")
        result = _validate_workspace_paths("cp", [safe_path], ws)
        assert result is None

    def test_absolute_path_outside_workspace(self, ws):
        """Arg with absolute path outside workspace → error string."""
        from tools.shell_tool import _validate_workspace_paths
        result = _validate_workspace_paths("cat", ["/etc/shadow"], ws)
        assert result is not None
        assert "outside" in result.lower()

    def test_dotdot_escape_three_levels(self, ws):
        """Arg with ../../../.env → error string (3+ levels up)."""
        from tools.shell_tool import _validate_workspace_paths
        result = _validate_workspace_paths("cat", ["../../../.env"], ws)
        assert result is not None

    def test_dotdot_one_level_safe(self, ws):
        """Arg with ../file (1 level up) → None (allowed)."""
        from tools.shell_tool import _validate_workspace_paths
        result = _validate_workspace_paths("cp", ["../other.py"], ws)
        assert result is None  # 1 level up is normal relative navigation

    def test_relative_safe_path(self, ws):
        """Arg with simple relative path → None (safe)."""
        from tools.shell_tool import _validate_workspace_paths
        result = _validate_workspace_paths("cp", ["src/main.py"], ws)
        assert result is None

    def test_none_workspace_skips_validation(self):
        """workspace_root=None → skips all checks → None."""
        from tools.shell_tool import _validate_workspace_paths
        result = _validate_workspace_paths("cat", ["/etc/shadow"], None)
        assert result is None

    def test_empty_arg_not_flagged(self, ws):
        """Empty string arg → not treated as a path."""
        from tools.shell_tool import _validate_workspace_paths
        result = _validate_workspace_paths("echo", ["", "hello"], ws)
        assert result is None


# ────────────────────────────────────────────────────────────────────────────
# M4: _ROOT_REMOVAL_PATTERNS sync
# ────────────────────────────────────────────────────────────────────────────

class TestRootRemovalPatterns:
    """Verify _ROOT_REMOVAL_PATTERNS is synced with _BLOCKED_PATTERNS."""

    def test_find_delete_in_root_removal(self):
        """find / -delete must be in _ROOT_REMOVAL_PATTERNS."""
        from hitl.pipeline import PermissionPipeline
        assert "find / -delete" in PermissionPipeline._ROOT_REMOVAL_PATTERNS

    def test_find_exec_rm_in_root_removal(self):
        """find / -exec rm must be in _ROOT_REMOVAL_PATTERNS."""
        from hitl.pipeline import PermissionPipeline
        assert "find / -exec rm" in PermissionPipeline._ROOT_REMOVAL_PATTERNS

    def test_chmod_000_in_root_removal(self):
        """chmod -R 000 / must be in _ROOT_REMOVAL_PATTERNS."""
        from hitl.pipeline import PermissionPipeline
        assert "chmod -R 000 /" in PermissionPipeline._ROOT_REMOVAL_PATTERNS

    def test_rm_rf_glob_in_root_removal(self):
        """rm -rf /* must already be in _ROOT_REMOVAL_PATTERNS."""
        from hitl.pipeline import PermissionPipeline
        assert "rm -rf /*" in PermissionPipeline._ROOT_REMOVAL_PATTERNS
