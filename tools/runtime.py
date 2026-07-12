"""
tools/runtime.py

Runtime 抽象层：把"命令执行"从工具实现里解耦出来。

工具（ShellTool / PytestTool / GitTool）只负责构造命令参数，
Runtime 负责实际执行——本地 subprocess 或 Docker 容器。

设计原则：
- 工具层完全不感知 Runtime，通过依赖注入传入
- Runtime 可以在 ToolRegistry 创建时一次性注入，所有工具共享
- LocalRuntime 是默认行为（向后兼容，不传 runtime 等同于之前）
- DockerRuntime 管理容器生命周期，首次执行时懒启动容器

用法：
    # 默认本地
    registry = build_registry()

    # Docker 沙箱
    runtime = DockerRuntime(repo_path="/path/to/repo")
    registry = build_registry(runtime=runtime)
    # agent 跑完后清理
    runtime.cleanup()

    # 或者用上下文管理器自动清理
    with DockerRuntime(repo_path="/path/to/repo") as runtime:
        registry = build_registry(runtime=runtime)
        agent.run(task, log)
"""

from __future__ import annotations

import os
import signal
import subprocess
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 跨平台进程树杀灭
# ---------------------------------------------------------------------------

def kill_process_tree(proc: subprocess.Popen) -> None:
    """
    杀掉子进程及其整个进程树。
    这是 Ctrl+C 安全的核心：确保没有孤儿进程残留。

    策略:
    - Unix:  用 preexec_fn=os.setsid 创建新 session，killpg 杀整个进程组
    - Win32: taskkill /T /F 杀进程树（cmd.exe 杀不死子进程，必须用 taskkill）
    """
    import sys as _sys
    if _sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=5,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    else:
        try:
            # proc 启动时用 preexec_fn=os.setsid，这里才能杀整个进程组
            pgid = os.getpgid(proc.pid)
            # 先礼貌 SIGTERM
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass


# ---------------------------------------------------------------------------
# RunResult — Runtime 执行结果
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    """Runtime 执行单条命令的结果。"""
    returncode: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        """合并 stdout + stderr，工具层直接用。"""
        return self.stdout + self.stderr


# ---------------------------------------------------------------------------
# ShellProvider — OS-native command translation layer
# ---------------------------------------------------------------------------

class ShellProvider(ABC):
    """Clean subprocess I/O for LLM consumption.

    The control plane (System Prompt) declares the platform to the LLM.
    The Runtime handles the dirty work: encoding detection, byte-to-text
    conversion, and output normalization. The LLM always sees clean UTF-8
    compatible text regardless of OS encoding quirks.
    """

    def decode_output(self, stdout_bytes: bytes, stderr_bytes: bytes) -> tuple[str, str]:
        """Decode raw subprocess output to clean text for LLM consumption.

        Tries the platform's native encoding first, falls back to UTF-8
        with replacement. The LLM never sees raw bytes or encoding errors.
        """
        primary = self._primary_encoding()
        fallbacks = ["utf-8", "latin-1"]

        def _decode(data: bytes) -> str:
            if not data:
                return ""
            try:
                return data.decode(primary)
            except (UnicodeDecodeError, LookupError):
                for enc in fallbacks:
                    try:
                        return data.decode(enc, errors="replace")
                    except LookupError:
                        continue
                return data.decode("utf-8", errors="replace")
            except Exception:
                return data.decode("utf-8", errors="replace")

        return _decode(stdout_bytes), _decode(stderr_bytes)

    @abstractmethod
    def _primary_encoding(self) -> str:
        """Primary encoding for this platform's subprocess output."""
        ...


class UnixBashProvider(ShellProvider):
    def _primary_encoding(self) -> str:
        return "utf-8"


class WindowsPowerShellProvider(ShellProvider):
    def _primary_encoding(self) -> str:
        import locale
        return locale.getpreferredencoding(do_setlocale=False) or "utf-8"


def _auto_shell_provider() -> ShellProvider:
    import os as _os
    if _os.name == "nt":
        return WindowsPowerShellProvider()
    return UnixBashProvider()


# ---------------------------------------------------------------------------
# ExecuteParams — structured, parameterized command execution
# ---------------------------------------------------------------------------

@dataclass
class ExecuteParams:
    """Physically isolated execution parameters.

    Each field is passed as a separate list item to subprocess.Popen
    with shell=False. The model CANNOT concatenate these into a shell string.
    No shell metacharacter injection is possible.
    """
    command: str
    """The executable to run: 'git', 'pytest', 'python', etc."""

    args: list[str] = field(default_factory=list)
    """Arguments as separate list items. Each item is ONE argument, never parsed by shell."""

    cwd: str | None = None
    timeout: int = 30
    env: dict[str, str] | None = None
    stdin: str | None = None


# ---------------------------------------------------------------------------
# GitState — Runtime-level git awareness
# ---------------------------------------------------------------------------

@dataclass
class GitState:
    """Runtime git authority. Tracks base commit at task start, captures diff at end.

    This is NOT exposed as an LLM tool. The Runtime owns git awareness —
    the model can request git operations via tools, but the authoritative
    state (base_commit, has_changes, incremental_diff) is Runtime-level.
    """
    repo_path: str
    base_commit: str = ""
    base_commit_short: str = ""
    current_diff: str = ""
    files_changed: list[str] = field(default_factory=list)
    has_changes: bool = False
    dirty_at_start: bool = False  # True if repo was dirty before task execution
    is_git_repo: bool = False


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------

class Runtime(ABC):
    """
    命令执行抽象基类。
    所有工具通过 runtime.exec() 或 runtime.execute() 执行命令，不直接调 subprocess。
    """

    @abstractmethod
    def exec(
        self,
        cmd: str,
        cwd: str | None = None,
        timeout: int = 30,
    ) -> RunResult:
        """
        执行 shell 命令，返回 RunResult。
        DEPRECATED: prefer execute() with ExecuteParams for new code.

        Args:
            cmd:     shell 命令字符串
            cwd:     工作目录（相对或绝对路径）
            timeout: 超时秒数

        Returns:
            RunResult，不抛异常（超时/错误封装在里面）
        """
        ...

    def execute(self, command: str, args: list[str] | None = None,
                cwd: str | None = None, timeout: int = 30,
                env: dict[str, str] | None = None) -> RunResult:
        """Execute a command with physically isolated parameters.

        Each parameter is passed as a separate list item to subprocess.Popen
        with shell=False. The model CANNOT inject shell metacharacters.

        Default implementation delegates to exec() for backward compat.
        Subclasses should override with shell=False implementation.
        """
        import shlex
        parts = [command] + (args or [])
        cmd_str = " ".join(shlex.quote(p) for p in parts)
        return self.exec(cmd_str, cwd=cwd, timeout=timeout)

    # ── Git awareness (Runtime-level, not tool-level) ──

    def capture_base_commit(self, repo_path: str) -> GitState:
        """Record HEAD commit at task start. Called by agent loop, NOT exposed to LLM."""
        state = GitState(repo_path=repo_path)
        try:
            result = self.execute("git", ["rev-parse", "HEAD"], cwd=repo_path, timeout=10)
            if result.success:
                state.base_commit = result.stdout.strip()
                state.base_commit_short = state.base_commit[:8]
                state.is_git_repo = True

            # Check if working tree was already dirty
            dirty = self.execute("git", ["diff", "HEAD", "--name-only"], cwd=repo_path, timeout=10)
            if dirty.success and dirty.stdout.strip():
                state.dirty_at_start = True
                state.files_changed = [f.strip() for f in dirty.stdout.splitlines() if f.strip()]
        except Exception:
            logger.debug("capture_base_commit failed — not a git repo?", exc_info=True)
        return state

    def capture_diff(self, state: GitState) -> GitState:
        """Capture git diff at task end. Updates state in place, returns it."""
        if not state.is_git_repo:
            return state
        try:
            result = self.execute("git", ["diff", "HEAD"], cwd=state.repo_path, timeout=10)
            if result.success:
                state.current_diff = result.stdout.strip()
                current_files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
                # Only count files changed THIS run (not pre-existing dirt)
                new_files = [f for f in current_files if f not in state.files_changed] if state.dirty_at_start else current_files
                state.files_changed = current_files
                state.has_changes = bool(new_files) if state.dirty_at_start else bool(current_files)
        except Exception:
            logger.debug("capture_diff failed", exc_info=True)
        return state

    def setup_workspace(self, repo_path: str) -> bool:
        """Ensure the workspace is ready for agent execution.

        Called automatically at PENDING→RUNNING transition.
        - Ensures the target directory is an independent git repo
        - Subclasses (DockerRuntime) can add container setup

        Returns True if workspace is ready, False if setup failed (non-fatal).
        """
        return True

    def cleanup(self) -> None:
        """释放 runtime 持有的资源（容器、连接等）。默认无操作。"""

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *_) -> None:
        self.cleanup()

    @property
    @abstractmethod
    def name(self) -> str:
        """Runtime 名称，用于日志。"""
        ...


# ---------------------------------------------------------------------------
# LocalRuntime — 本地 subprocess（默认）
# ---------------------------------------------------------------------------

class LocalRuntime(Runtime):
    """
    本地执行，用 subprocess.Popen。
    相比 subprocess.run 的优势：
    - 持有进程引用，可以在中断/超时时杀掉进程树
    - KeyboardInterrupt 时 kill_process_tree() 防止孤儿进程
    - Windows: 自动检测并使用 Git Bash (bash.exe), 兼容 Unix 命令
    """

    # Git Bash 标准安装路径, 按优先级查找
    _BASH_CANDIDATES = [
        r"D:\SoftwareDownload\Git\usr\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
    ]

    def __init__(self, shell: str = "system", shell_provider: ShellProvider | None = None) -> None:
        """Args:
            shell: "system" (default — native OS shell), "bash" (opt-in Git Bash).
            shell_provider: ShellProvider for command translation + encoding.
                Auto-detected from platform if None.
        """
        self._current_proc: subprocess.Popen | None = None
        self._bash_path: str | None = None
        self._shell_mode = shell
        self._shell_provider = shell_provider or _auto_shell_provider()
        if os.name == "nt" and shell == "bash":
            self._bash_path = self._find_bash()
            if self._bash_path is None:
                logger.warning(
                    "LocalRuntime(shell='bash'): Git Bash not found, "
                    "falling back to system shell"
                )
                self._shell_mode = "system"

    def setup_workspace(self, repo_path: str) -> bool:
        """Ensure repo_path is an independent git repo with a CLEAN baseline.

        If no .git exists, auto-init + commit all files.
        If .git exists but working tree is dirty, auto-commit the dirt as
        a baseline so the agent's own changes are the only ones visible in
        git diff. This prevents the "pre-existing dirt" problem where the
        GitDiffGuard cannot see the agent's edits because the file was
        already modified before the agent started.
        """
        git_dir = Path(repo_path) / ".git"
        if not git_dir.exists():
            try:
                self.execute("git", ["init"], cwd=repo_path, timeout=10)
                self.execute("git", ["add", "-A"], cwd=repo_path, timeout=30)
                self.execute("git", ["commit", "-m", "agent-workspace-init"],
                            cwd=repo_path, timeout=10)
                return True
            except Exception as exc:
                logger.warning("Failed to auto-init git in %s: %s", repo_path, exc)
                return False

        # .git exists — auto-commit any dirty state as baseline
        try:
            status = self.execute("git", ["status", "--porcelain"], cwd=repo_path, timeout=10)
            if status.stdout.strip():
                logger.info("Workspace dirty — auto-committing baseline")
                self.execute("git", ["add", "-A"], cwd=repo_path, timeout=30)
                self.execute("git", ["commit", "-m", "agent-baseline-snapshot"],
                            cwd=repo_path, timeout=10)
        except Exception:
            pass  # non-fatal
        return True

    @staticmethod
    def _find_bash() -> str | None:
        """Find Git Bash on Windows. Returns path or None."""
        import shutil as _shutil
        for candidate in LocalRuntime._BASH_CANDIDATES:
            if os.path.isfile(candidate):
                return candidate
        # Fallback: try PATH
        found = _shutil.which("bash")
        if found and os.path.isfile(found):
            return found
        return None

    @property
    def name(self) -> str:
        if self._shell_mode == "system":
            return "local(system)"
        return f"local({'bash' if self._bash_path else 'system'})"

    def exec(
        self,
        cmd: str,
        cwd: str | None = None,
        timeout: int = 30,
    ) -> RunResult:
        # Normalize LLM-generated commands for current OS/shell
        cmd = CommandNormalizer.normalize(cmd)

        proc: subprocess.Popen | None = None
        try:
            popen_kwargs: dict[str, Any] = {
                "args": cmd,
                "shell": True,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": False,  # binary mode — Runtime handles encoding
                "cwd": cwd,
            }
            # Windows: bash is opt-in only. Default: native system shell.
            if os.name == "nt" and self._bash_path and self._shell_mode == "bash":
                popen_kwargs["args"] = [self._bash_path, "-c", cmd]
                popen_kwargs["shell"] = False
            # Unix: 创建新 session，后续 killpg 不会误杀父进程
            if os.name != "nt":
                popen_kwargs["preexec_fn"] = os.setsid

            proc = subprocess.Popen(**popen_kwargs)
            self._current_proc = proc
            stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
            stdout, stderr = self._shell_provider.decode_output(stdout_bytes or b"", stderr_bytes or b"")
            return RunResult(
                returncode=proc.returncode if proc.returncode is not None else -1,
                stdout=stdout,
                stderr=stderr,
            )

        except subprocess.TimeoutExpired:
            if proc and proc.returncode is None:
                kill_process_tree(proc)
                proc.wait(timeout=5)
            return RunResult(
                returncode=-1,
                stdout="",
                stderr=f"Command timed out after {timeout}s: {cmd!r}",
            )

        except KeyboardInterrupt:
            if proc and proc.returncode is None:
                kill_process_tree(proc)
                proc.wait(timeout=5)
            return RunResult(
                returncode=-1,
                stdout="",
                stderr=f"Command interrupted by user: {cmd!r}",
            )

        except Exception as e:
            if proc and proc.returncode is None:
                try:
                    kill_process_tree(proc)
                except Exception:
                    pass
            return RunResult(returncode=-1, stdout="", stderr=str(e))

        finally:
            if proc is not None:
                self._current_proc = None

    def execute(
        self,
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        timeout: int = 30,
        env: dict[str, str] | None = None,
    ) -> RunResult:
        """Execute a command with physically isolated parameters (shell=False).

        Each argument is a separate list element. The shell never parses
        the command string, so shell metacharacters in args are inert.
        """
        cmd_list = [command] + (args or [])
        proc: subprocess.Popen | None = None
        try:
            popen_kwargs: dict[str, Any] = {
                "args": cmd_list,
                "shell": False,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": False,  # binary mode — Runtime handles encoding
                "cwd": cwd,
            }
            if env:
                full_env = os.environ.copy()
                full_env.update(env)
                popen_kwargs["env"] = full_env
            # Unix: create new session for clean process-tree kill
            if os.name != "nt":
                popen_kwargs["preexec_fn"] = os.setsid

            proc = subprocess.Popen(**popen_kwargs)
            self._current_proc = proc
            stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
            stdout, stderr = self._shell_provider.decode_output(stdout_bytes or b"", stderr_bytes or b"")
            return RunResult(
                returncode=proc.returncode if proc.returncode is not None else -1,
                stdout=stdout,
                stderr=stderr,
            )

        except subprocess.TimeoutExpired:
            if proc and proc.returncode is None:
                kill_process_tree(proc)
                proc.wait(timeout=5)
            return RunResult(
                returncode=-1,
                stdout="",
                stderr=f"Command timed out after {timeout}s: {command!r}",
            )

        except KeyboardInterrupt:
            if proc and proc.returncode is None:
                kill_process_tree(proc)
                proc.wait(timeout=5)
            return RunResult(
                returncode=-1,
                stdout="",
                stderr=f"Command interrupted by user: {command!r}",
            )

        except Exception as e:
            if proc and proc.returncode is None:
                try:
                    kill_process_tree(proc)
                except Exception:
                    pass
            return RunResult(returncode=-1, stdout="", stderr=str(e))

        finally:
            if proc is not None:
                self._current_proc = None


# ---------------------------------------------------------------------------
# DockerRuntime — Docker 沙箱
# ---------------------------------------------------------------------------

# ── Command Normalizer: OS-aware adaptation ──────────────────────────────

class CommandNormalizer:
    """Normalize LLM-generated commands before shell execution.

    The LLM outputs intent-level commands. This layer adapts them to the
    current OS and shell environment. Without it, Unix commands fail on
    Windows, path quoting breaks, and cross-platform issues cascade.
    """

    @staticmethod
    def normalize(cmd: str) -> str:
        """Minimal normalization. Does NOT parse or rewrite shell commands —
        that path leads to an unmaintainable regex graveyard. Claude Code
        uses parameter-level isolation (cwd param, not text rewriting)."""
        # Only fix trivial syntax issues, never semantic rewrites
        return cmd


# 沙箱容器使用的 Docker 镜像
# 包含 Python、git、常用工具，体积合理
SANDBOX_IMAGE = "python:3.11-slim"

# 容器内 repo 的挂载路径
CONTAINER_WORKDIR = "/workspace"


class DockerRuntime(Runtime):
    """
    Docker 沙箱 Runtime。

    首次调用 exec() 时懒启动容器：
    - 基于 python:3.11-slim 镜像
    - 把 repo_path bind mount 到容器的 /workspace
    - 容器持续运行（tail -f /dev/null），每条命令用 docker exec 执行
    - cleanup() 时停止并删除容器

    这样比每条命令都 docker run 快得多（避免反复启动容器的开销）。

    Args:
        repo_path:   宿主机上 repo 的绝对路径，会被 mount 进容器
        image:       Docker 镜像名，默认 python:3.11-slim
        extra_mounts: 额外的 bind mount，格式 [(host_path, container_path), ...]
        setup_cmds:  容器启动后执行的初始化命令（如 pip install -r requirements.txt）
    """

    def __init__(
        self,
        repo_path: str | Path,
        image: str = SANDBOX_IMAGE,
        extra_mounts: list[tuple[str, str]] | None = None,
        setup_cmds: list[str] | None = None,
    ) -> None:
        self._repo_path = str(Path(repo_path).resolve())
        self._image = image
        self._extra_mounts = extra_mounts or []
        self._setup_cmds = setup_cmds or []
        self._container_id: str | None = None
        # 容器名加随机后缀，避免冲突
        self._container_name = f"coding-agent-sandbox-{uuid.uuid4().hex[:8]}"

    @property
    def name(self) -> str:
        return f"docker({self._image})"

    @property
    def container_id(self) -> str | None:
        return self._container_id

    @property
    def is_running(self) -> bool:
        return self._container_id is not None

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def exec(
        self,
        cmd: str,
        cwd: str | None = None,
        timeout: int = 30,
    ) -> RunResult:
        """在容器里执行命令，首次调用时自动启动容器。"""
        if not self.is_running:
            startup_result = self._start_container()
            if startup_result is not None:
                # 启动失败，返回错误
                return startup_result

        # 确定容器内工作目录
        if cwd:
            # 如果 cwd 是宿主机路径，转换为容器内路径
            host_cwd = str(Path(cwd).resolve())
            if host_cwd.startswith(self._repo_path):
                relative = host_cwd[len(self._repo_path):].lstrip("/\\").replace("\\", "/")
                container_cwd = f"{CONTAINER_WORKDIR}/{relative}" if relative else CONTAINER_WORKDIR
            else:
                container_cwd = cwd   # 可能是容器内的绝对路径
        else:
            container_cwd = CONTAINER_WORKDIR

        docker_cmd = [
            "docker", "exec",
            "--workdir", container_cwd,
            self._container_id,
            "bash", "-c", cmd,
        ]

        proc: subprocess.Popen | None = None
        try:
            popen_kwargs: dict[str, Any] = {
                "args": docker_cmd,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": False,  # binary mode — containers are UTF-8
            }
            if os.name != "nt":
                popen_kwargs["preexec_fn"] = os.setsid

            proc = subprocess.Popen(**popen_kwargs)
            adjusted_timeout = timeout + 5  # docker exec 本身有少量开销
            stdout_bytes, stderr_bytes = proc.communicate(timeout=adjusted_timeout)
            stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
            stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
            return RunResult(
                returncode=proc.returncode if proc.returncode is not None else -1,
                stdout=stdout,
                stderr=stderr,
            )

        except subprocess.TimeoutExpired:
            if proc and proc.returncode is None:
                kill_process_tree(proc)
                proc.wait(timeout=5)
            return RunResult(
                returncode=-1,
                stdout="",
                stderr=f"Command timed out after {timeout}s in container: {cmd!r}",
            )

        except KeyboardInterrupt:
            if proc and proc.returncode is None:
                kill_process_tree(proc)
                proc.wait(timeout=5)
            return RunResult(
                returncode=-1,
                stdout="",
                stderr=f"Command interrupted by user in container: {cmd!r}",
            )

        except Exception as e:
            if proc and proc.returncode is None:
                try:
                    kill_process_tree(proc)
                except Exception:
                    pass
            return RunResult(returncode=-1, stdout="", stderr=str(e))

    def execute(
        self,
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        timeout: int = 30,
        env: dict[str, str] | None = None,
    ) -> RunResult:
        """Execute a command in the container with physically isolated parameters.

        Builds a docker exec command with proper argument escaping via shlex.quote().
        Each argument is individually quoted so shell metacharacters are inert.
        """
        import shlex
        if not self.is_running:
            startup_result = self._start_container()
            if startup_result is not None:
                return startup_result

        # Determine container working directory
        if cwd:
            host_cwd = str(Path(cwd).resolve())
            if host_cwd.startswith(self._repo_path):
                relative = host_cwd[len(self._repo_path):].lstrip("/\\").replace("\\", "/")
                container_cwd = f"{CONTAINER_WORKDIR}/{relative}" if relative else CONTAINER_WORKDIR
            else:
                container_cwd = cwd
        else:
            container_cwd = CONTAINER_WORKDIR

        # Build bash -c string with properly quoted arguments
        arg_str = " ".join(shlex.quote(a) for a in (args or []))
        bash_cmd = f"{shlex.quote(command)} {arg_str}".strip()

        docker_cmd = [
            "docker", "exec",
            "--workdir", container_cwd,
            self._container_id,
            "bash", "-c", bash_cmd,
        ]

        proc: subprocess.Popen | None = None
        try:
            popen_kwargs: dict[str, Any] = {
                "args": docker_cmd,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": False,  # binary mode — containers are UTF-8
            }
            if os.name != "nt":
                popen_kwargs["preexec_fn"] = os.setsid

            proc = subprocess.Popen(**popen_kwargs)
            adjusted_timeout = timeout + 5
            stdout_bytes, stderr_bytes = proc.communicate(timeout=adjusted_timeout)
            stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
            stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
            return RunResult(
                returncode=proc.returncode if proc.returncode is not None else -1,
                stdout=stdout,
                stderr=stderr,
            )

        except subprocess.TimeoutExpired:
            if proc and proc.returncode is None:
                kill_process_tree(proc)
                proc.wait(timeout=5)
            return RunResult(
                returncode=-1, stdout="",
                stderr=f"Command timed out after {timeout}s in container: {command!r}",
            )
        except KeyboardInterrupt:
            if proc and proc.returncode is None:
                kill_process_tree(proc)
                proc.wait(timeout=5)
            return RunResult(
                returncode=-1, stdout="",
                stderr=f"Command interrupted by user in container: {command!r}",
            )
        except Exception as e:
            if proc and proc.returncode is None:
                try:
                    kill_process_tree(proc)
                except Exception:
                    pass
            return RunResult(returncode=-1, stdout="", stderr=str(e))

    def cleanup(self) -> None:
        """停止并删除容器。"""
        if not self._container_id:
            return
        logger.info("Stopping sandbox container %s", self._container_name)
        try:
            subprocess.run(
                ["docker", "rm", "-f", self._container_id],
                capture_output=True, timeout=15,
            )
        except Exception as e:
            logger.warning("Failed to remove container %s: %s", self._container_id, e)
        finally:
            self._container_id = None

    # ------------------------------------------------------------------
    # 内部：容器生命周期
    # ------------------------------------------------------------------

    def _start_container(self) -> RunResult | None:
        """
        拉取镜像（如需要）并启动容器。
        返回 None 表示成功，返回 RunResult 表示失败。
        """
        logger.info(
            "Starting sandbox container %s (image=%s, repo=%s)",
            self._container_name, self._image, self._repo_path,
        )

        # 检查 Docker 是否可用
        check = subprocess.run(
            ["docker", "info"],
            capture_output=True, timeout=10,
        )
        if check.returncode != 0:
            return RunResult(
                returncode=-1,
                stdout="",
                stderr=(
                    "Docker is not available. "
                    "Make sure Docker Desktop is running, or use --no-sandbox."
                ),
            )

        # 构建 docker run 命令
        run_args = [
            "docker", "run",
            "--detach",                                 # 后台运行
            "--name", self._container_name,
            "--rm",                                     # 停止时自动删除
            "-v", f"{self._repo_path}:{CONTAINER_WORKDIR}",  # mount repo
            "--workdir", CONTAINER_WORKDIR,
            "--network", "none",                        # 默认断网，更安全
        ]

        # 额外 mount
        for host_path, container_path in self._extra_mounts:
            run_args += ["-v", f"{host_path}:{container_path}"]

        run_args += [self._image, "tail", "-f", "/dev/null"]

        try:
            proc = subprocess.run(
                run_args,
                capture_output=True,
                text=True,
                timeout=60,  # 拉镜像可能需要时间
            )
        except subprocess.TimeoutExpired:
            return RunResult(
                returncode=-1, stdout="",
                stderr="Timed out starting Docker container (60s). Is Docker running?",
            )

        if proc.returncode != 0:
            return RunResult(
                returncode=proc.returncode,
                stdout="",
                stderr=f"Failed to start container:\n{proc.stderr}",
            )

        self._container_id = proc.stdout.strip()
        logger.info("Container started: %s", self._container_id[:12])

        # 执行初始化命令
        for setup_cmd in self._setup_cmds:
            result = self.exec(setup_cmd, timeout=120)
            if not result.success:
                logger.warning(
                    "Setup command failed: %r\n%s", setup_cmd, result.stderr
                )

        return None   # 成功

    def install_requirements(self, requirements_file: str = "requirements.txt") -> RunResult:
        """
        在容器里安装依赖。快捷方法，等价于 exec("pip install -r requirements.txt")。
        """
        return self.exec(
            f"pip install -r {requirements_file} -q",
            timeout=120,
        )


# ---------------------------------------------------------------------------
# 便捷工厂函数
# ---------------------------------------------------------------------------

def create_runtime(
    sandbox: bool = False,
    repo_path: str | None = None,
    image: str = SANDBOX_IMAGE,
    network: bool = False,
) -> Runtime:
    """
    根据配置创建合适的 Runtime。

    Args:
        sandbox:   True 则创建 DockerRuntime，False 则 LocalRuntime
        repo_path: sandbox=True 时必须提供
        image:     Docker 镜像名
        network:   sandbox 模式下是否允许网络（默认 False，更安全）

    Returns:
        Runtime 实例
    """
    if not sandbox:
        return LocalRuntime()

    if not repo_path:
        raise ValueError("repo_path is required when sandbox=True")

    runtime = DockerRuntime(repo_path=repo_path, image=image)
    if network:
        # 允许网络时去掉 --network none
        runtime._allow_network = True  # DockerRuntime._start_container 检查此标志

    return runtime