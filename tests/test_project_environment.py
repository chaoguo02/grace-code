from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from runtime.project_environment import (
    CapabilitySnapshot,
    ExecutableKind,
    ExecutableSource,
    ProjectExecutableResolver,
)
from tools.runtime import RunResult, Runtime
from tools.base import ToolErrorType
from tools.test_tool import PytestTool


def _make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o755)
    return path.resolve()


def test_resolver_ignores_host_path(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    global_bin = tmp_path / "global-bin"
    _make_executable(global_bin / ("python.exe" if os.name == "nt" else "python"))
    monkeypatch.setenv("PATH", str(global_bin))

    resolver = ProjectExecutableResolver(project_root=project)

    assert resolver.resolve(ExecutableKind.PYTHON) is None


def test_resolver_returns_absolute_project_virtualenv_python(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    relative = ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
    expected = _make_executable(project / relative)

    resolved = ProjectExecutableResolver(project_root=project).resolve(ExecutableKind.PYTHON)

    assert resolved is not None
    assert resolved.path == expected
    assert resolved.path.is_absolute()
    assert resolved.source is ExecutableSource.PROJECT


def test_resolver_accepts_only_absolute_injected_paths(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    injected = _make_executable(tmp_path / "runtime" / ("python.exe" if os.name == "nt" else "python"))

    resolver = ProjectExecutableResolver(
        project_root=project,
        injected={ExecutableKind.PYTHON: injected},
    )
    resolved = resolver.resolve(ExecutableKind.PYTHON)
    assert resolved is not None
    assert resolved.path == injected
    assert resolved.source is ExecutableSource.INJECTED

    with pytest.raises(ValueError, match="must be absolute"):
        ProjectExecutableResolver(
            project_root=project,
            injected={ExecutableKind.PYTHON: Path("python")},
        )


def test_capability_snapshot_does_not_report_global_python(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    global_bin = tmp_path / "global-bin"
    _make_executable(global_bin / ("python.exe" if os.name == "nt" else "python"))
    monkeypatch.setenv("PATH", str(global_bin))

    snapshot = CapabilitySnapshot.probe(str(project))

    assert snapshot.python_available is False
    assert snapshot.pytest_available is False


class _DeclaredPythonRuntime(Runtime):
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], str | None]] = []

    @property
    def name(self) -> str:
        return "declared-python"

    def resolve_executable(self, kind):
        if kind is ExecutableKind.PYTHON:
            return str(Path(sys.executable).resolve())
        return None

    def exec(self, cmd: str, cwd: str | None = None, timeout: int = 30) -> RunResult:
        raise AssertionError("PytestTool must use parameterized execute()")

    def execute(
        self,
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        timeout: int = 30,
        env: dict[str, str] | None = None,
    ) -> RunResult:
        self.calls.append((command, list(args or []), cwd))
        return RunResult(returncode=0, stdout="1 passed", stderr="")


def test_pytest_tool_uses_runtime_declared_absolute_python(tmp_path):
    runtime = _DeclaredPythonRuntime()
    tool = PytestTool(runtime=runtime, workspace_root=tmp_path)

    result = tool.execute({"path": "."})

    assert result.success is True
    command, args, cwd = runtime.calls[0]
    assert Path(command).is_absolute()
    assert args[:2] == ["-m", "pytest"]
    assert cwd == str(tmp_path.resolve())


def test_pytest_tool_rejects_cwd_outside_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    runtime = _DeclaredPythonRuntime()
    tool = PytestTool(runtime=runtime, workspace_root=project)

    result = tool.execute({"cwd": str(tmp_path)})

    assert result.success is False
    assert result.tool_error is not None
    assert result.tool_error.error_type is ToolErrorType.PERMISSION_DENIED
    assert runtime.calls == []
