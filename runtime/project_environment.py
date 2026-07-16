"""Project-scoped executable resolution for local Runtime processes."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping


class ExecutableKind(str, Enum):
    PYTHON = "python"
    BASH = "bash"


class ExecutableSource(str, Enum):
    PROJECT = "project"
    INJECTED = "injected"


@dataclass(frozen=True)
class ResolvedExecutable:
    kind: ExecutableKind
    path: Path
    source: ExecutableSource


_PROJECT_CANDIDATES: Mapping[ExecutableKind, tuple[str, ...]] = {
    ExecutableKind.PYTHON: (
        ".venv/Scripts/python.exe",
        ".venv/bin/python",
        "venv/Scripts/python.exe",
        "venv/bin/python",
    ),
    ExecutableKind.BASH: (
        ".tools/bin/bash.exe",
        ".tools/bin/bash",
    ),
}


@dataclass(frozen=True)
class ProjectExecutableResolver:
    """Resolve executables without consulting the host's global PATH.

    Entry layers may inject an absolute Runtime-managed executable. Otherwise
    only declarative candidates physically contained by the target project are
    considered.
    """

    project_root: Path
    injected: Mapping[ExecutableKind, Path] = field(default_factory=dict)

    def __post_init__(self) -> None:
        root = Path(self.project_root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"project root is not a directory: {root}")
        object.__setattr__(self, "project_root", root)

        normalized: dict[ExecutableKind, Path] = {}
        for kind, raw_path in self.injected.items():
            if not isinstance(kind, ExecutableKind):
                raise TypeError("injected executable keys must be ExecutableKind values")
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                raise ValueError(f"injected {kind.value} path must be absolute: {path}")
            normalized[kind] = path.resolve()
        object.__setattr__(self, "injected", normalized)

    def resolve(self, kind: ExecutableKind) -> ResolvedExecutable | None:
        if not isinstance(kind, ExecutableKind):
            raise TypeError("kind must be an ExecutableKind")

        injected = self.injected.get(kind)
        if injected is not None:
            return self._fact(kind, injected, ExecutableSource.INJECTED)

        for relative in _PROJECT_CANDIDATES[kind]:
            candidate = (self.project_root / relative).resolve()
            try:
                candidate.relative_to(self.project_root)
            except ValueError:
                continue
            fact = self._fact(kind, candidate, ExecutableSource.PROJECT)
            if fact is not None:
                return fact
        return None

    @staticmethod
    def _fact(
        kind: ExecutableKind,
        path: Path,
        source: ExecutableSource,
    ) -> ResolvedExecutable | None:
        if not path.is_file():
            return None
        if os.name != "nt" and not os.access(path, os.X_OK):
            return None
        return ResolvedExecutable(kind=kind, path=path, source=source)


@dataclass(frozen=True)
class CapabilitySnapshot:
    """Project-scoped environment facts rendered for the model."""

    python_available: bool = False
    pytest_available: bool = False
    git_available: bool = False
    bash_available: bool = False
    repo_dirty: bool = False
    os_name: str = ""

    @classmethod
    def probe(
        cls,
        project_root: str | Path,
        resolver: ProjectExecutableResolver | None = None,
    ) -> "CapabilitySnapshot":
        import subprocess

        from runtime.workspace_facts import capture_workspace_snapshot

        root = Path(project_root).expanduser().resolve()
        environment = resolver or ProjectExecutableResolver(project_root=root)
        python = environment.resolve(ExecutableKind.PYTHON)
        bash = environment.resolve(ExecutableKind.BASH)

        pytest_available = False
        if python is not None:
            try:
                result = subprocess.run(
                    [str(python.path), "-m", "pytest", "--version"],
                    capture_output=True,
                    timeout=5,
                    cwd=root,
                    check=False,
                )
                pytest_available = result.returncode == 0
            except (OSError, subprocess.SubprocessError):
                pass

        workspace = capture_workspace_snapshot(root)
        os_name = "win32" if os.name == "nt" else os.uname().sysname.lower()
        return cls(
            python_available=python is not None,
            pytest_available=pytest_available,
            git_available=workspace.is_git_repo,
            bash_available=bash is not None,
            repo_dirty=bool(workspace.files or workspace.current_patch),
            os_name=os_name,
        )

    def render_for_agent(self) -> str:
        def yes_no(value: bool) -> str:
            return "yes" if value else "no"

        return (
            f"[ENVIRONMENT] os={self.os_name} python={yes_no(self.python_available)} "
            f"pytest={yes_no(self.pytest_available)} git={yes_no(self.git_available)} "
            f"bash={yes_no(self.bash_available)} repo_dirty={yes_no(self.repo_dirty)}"
        )
