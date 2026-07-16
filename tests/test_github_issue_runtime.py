from __future__ import annotations

from pathlib import Path

import pytest

from entry.github_issue import clone_repo, create_branch, push_branch
from tools.runtime import RunResult, Runtime


class RecordingRuntime(Runtime):
    def __init__(self, results: list[RunResult] | None = None) -> None:
        self.calls: list[tuple[str, list[str], str | None, int]] = []
        self._results = list(results or [])

    @property
    def name(self) -> str:
        return "recording"

    def exec(
        self,
        cmd: str,
        cwd: str | None = None,
        timeout: int = 30,
        stdin_data: str | None = None,
    ) -> RunResult:
        raise AssertionError("GitHub issue operations must use parameterized execute()")

    def execute(
        self,
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        timeout: int = 30,
        env: dict[str, str] | None = None,
    ) -> RunResult:
        self.calls.append((command, list(args or []), cwd, timeout))
        if self._results:
            return self._results.pop(0)
        return RunResult(returncode=0, stdout="ok", stderr="")


def _failure(message: str = "failed") -> RunResult:
    return RunResult(returncode=1, stdout="", stderr=message)


def test_clone_uses_target_parent_as_absolute_runtime_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    runtime = RecordingRuntime()
    target = tmp_path / "repos" / "project"
    target.parent.mkdir()

    clone_repo("owner/project", str(target), runtime)

    assert runtime.calls == [
        (
            "git",
            ["clone", "https://github.com/owner/project.git", "project"],
            str(target.parent.resolve()),
            60,
        )
    ]


def test_create_branch_falls_back_through_same_runtime(tmp_path: Path) -> None:
    runtime = RecordingRuntime([_failure(), RunResult(0, "", "")])

    create_branch(str(tmp_path), "agent/fix", runtime)

    assert [call[1] for call in runtime.calls] == [
        ["checkout", "-b", "agent/fix"],
        ["checkout", "agent/fix"],
    ]
    assert all(call[2] == str(tmp_path.resolve()) for call in runtime.calls)


def test_create_branch_reports_failed_fallback(tmp_path: Path) -> None:
    runtime = RecordingRuntime([_failure("already exists"), _failure("cannot switch")])

    with pytest.raises(RuntimeError, match="cannot switch"):
        create_branch(str(tmp_path), "agent/fix", runtime)


def test_push_failure_reports_runtime_output(tmp_path: Path) -> None:
    runtime = RecordingRuntime([_failure("remote rejected")])

    with pytest.raises(RuntimeError, match="remote rejected"):
        push_branch(str(tmp_path), "agent/fix", runtime)
