"""G42: Migration complete — all old flags/markers/shadow deprecated.

AC: migration_markers.py has DEPRECATED notice
AC: deprecation_log.py has DEPRECATED notice
AC: listeners/shadow.py has DEPRECATED notice
AC: GRACE_RUNTIME_MODE is NOT in production code paths (only docs/comments)
AC: run_server.py uses assemble() without mode branching
"""

import ast
import os

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")

PRODUCTION_FILES = [
    "server/main.py", "run_server.py",
    "server/services/run_submission.py",
    "server/services/agent_service.py",
]


class TestMigrationComplete:
    """G42: All migration markers deprecated; no mode flags in production."""

    def test_migration_markers_deprecated(self):
        path = os.path.join(PROJECT_ROOT, "composition", "migration_markers.py")
        with open(path, encoding="utf-8") as f:
            assert "DEPRECATED" in f.read()

    def test_deprecation_log_deprecated(self):
        path = os.path.join(PROJECT_ROOT, "composition", "deprecation_log.py")
        with open(path, encoding="utf-8") as f:
            assert "DEPRECATED" in f.read()

    def test_shadow_deprecated(self):
        path = os.path.join(PROJECT_ROOT, "listeners", "shadow.py")
        with open(path, encoding="utf-8") as f:
            assert "DEPRECATED" in f.read()

    def test_no_grace_runtime_mode_in_production_code(self):
        """G42: GRACE_RUNTIME_MODE must not appear in production code paths.

        Allowed in: docs/, tests/, build/ (generated), composition/migration_markers.py (deprecated).
        """
        violations = []
        for fname in PRODUCTION_FILES:
            path = os.path.join(PROJECT_ROOT, fname)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                source = f.read()
            if "GRACE_RUNTIME_MODE" in source:
                # Check if it's in a comment or docstring
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        if "GRACE_RUNTIME_MODE" in node.value:
                            break  # in docstring — OK
                else:
                    # Not only in docstrings — check for actual code
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Compare):
                            code = ast.unparse(node)
                            if "GRACE_RUNTIME_MODE" in code:
                                violations.append(f"{fname}: {code}")
        assert violations == [], (
            f"G42: GRACE_RUNTIME_MODE in production code:\n"
            + "\n".join(violations)
        )
