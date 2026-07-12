"""Task Intent — structured execution intent for the Runtime layer.

Claude Code Intent Layer pattern: the agent's mission is NOT a string.
It's a typed object that the Runtime (MacroLoopDetector, CompletionGuard,
PermissionPipeline) uses to make decisions without guessing.

Before this: MacroLoopDetector used a string "edit"/"analysis" that was
never wired. Progress was defined only by file writes — fatal for Plan agent.

After this: TaskIntent is created at the entry layer, injected into
RuntimeController and MacroLoopDetector at construction time. Every
Runtime component knows EXACTLY what progress means for this task.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class IntentPhase(str, Enum):
    ANALYSIS = "analysis"    # read, search, explore — writes are NOT progress
    EXECUTION = "execution"  # edit, write, test — only writes/validates count


@dataclass(frozen=True)
class TaskIntent:
    """Immutable execution intent. Created by the entry layer, enforced
    by the Runtime. The agent has no opportunity to override.

    phase: ANALYSIS → reading new files = progress. EXECUTION → writes = progress.
    """

    phase: IntentPhase = IntentPhase.EXECUTION

    # ── Factory presets ──────────────────────────────────────────────

    @classmethod
    def for_plan(cls) -> "TaskIntent":
        """Plan agent: analysis phase — exploration IS progress."""
        return cls(phase=IntentPhase.ANALYSIS)

    @classmethod
    def for_build(cls) -> "TaskIntent":
        """Build agent: execution phase — only writes count."""
        return cls(phase=IntentPhase.EXECUTION)

    @classmethod
    def from_string(cls, value: str) -> "TaskIntent":
        """Backward-compatible: "analysis"/"edit" → TaskIntent."""
        if value == "analysis":
            return cls.for_plan()
        return cls.for_build()

    # ── Queries ──────────────────────────────────────────────────────

    @property
    def is_analysis(self) -> bool:
        return self.phase == IntentPhase.ANALYSIS

    @property
    def is_execution(self) -> bool:
        return self.phase == IntentPhase.EXECUTION


# ── Capability Snapshot: environment facts as Runtime input ──────────────

@dataclass
class CapabilitySnapshot:
    """Deterministically probed environment facts. Injected into the agent
    before execution so the model never has to guess what's available.

    This is NOT reactive (like CapabilityRegistry) — it's proactive.
    The Runtime tells the agent what tools are available BEFORE it tries them.
    """

    python_available: bool = True
    pytest_available: bool = False
    git_available: bool = True
    bash_available: bool = False
    repo_dirty: bool = False
    os_name: str = ""

    @classmethod
    def probe(cls, repo_path: str = ".") -> "CapabilitySnapshot":
        """Run deterministic pre-flight checks. Returns a snapshot of
        what's actually available in the current environment."""
        import os as _os
        import shutil as _shutil
        import subprocess as _sp

        # OS detection
        _os_name = "win32" if _os.name == "nt" else _os.uname().sysname.lower()

        # Python
        _python_ok = _shutil.which("python") is not None or _shutil.which("python3") is not None

        # Pytest — runtime probe in CWD. Never trust config files.
        # Can it actually RUN? That's the only question.
        _pytest_ok = False
        try:
            _result = _sp.run(
                ["python", "-m", "pytest", "--version"],
                capture_output=True, timeout=5, cwd=repo_path,
            )
            _pytest_ok = _result.returncode == 0
        except Exception:
            pass

        # Git
        _git_ok = _shutil.which("git") is not None

        # Bash (Git Bash on Windows, native bash on Unix)
        _bash_ok = _shutil.which("bash") is not None

        # Repo dirty check
        _dirty = False
        if _git_ok:
            try:
                _result = _sp.run(
                    ["git", "status", "--porcelain"],
                    capture_output=True, timeout=5, cwd=repo_path,
                )
                _dirty = bool(_result.stdout.strip())
            except Exception:
                pass

        return cls(
            python_available=_python_ok,
            pytest_available=_pytest_ok,
            git_available=_git_ok,
            bash_available=_bash_ok,
            repo_dirty=_dirty,
            os_name=_os_name,
        )

    def render_for_agent(self) -> str:
        """Format as a one-line system message for the agent."""
        def _yn(b: bool) -> str: return "yes" if b else "no"
        return (
            f"[ENVIRONMENT] os={self.os_name} python={_yn(self.python_available)} "
            f"pytest={_yn(self.pytest_available)} git={_yn(self.git_available)} "
            f"bash={_yn(self.bash_available)} repo_dirty={_yn(self.repo_dirty)}"
        )
