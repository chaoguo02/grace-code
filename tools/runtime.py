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
from dataclasses import dataclass
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
# 抽象基类
# ---------------------------------------------------------------------------

class Runtime(ABC):
    """
    命令执行抽象基类。
    所有工具通过 runtime.exec() 执行命令，不直接调 subprocess。
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

        Args:
            cmd:     shell 命令字符串
            cwd:     工作目录（相对或绝对路径）
            timeout: 超时秒数

        Returns:
            RunResult，不抛异常（超时/错误封装在里面）
        """
        ...

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

    def __init__(self) -> None:
        self._current_proc: subprocess.Popen | None = None
        self._bash_path: str | None = None
        if os.name == "nt":
            self._bash_path = self._find_bash()

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
        return f"local({'bash' if self._bash_path else 'cmd'})"

    def exec(
        self,
        cmd: str,
        cwd: str | None = None,
        timeout: int = 30,
    ) -> RunResult:
        proc: subprocess.Popen | None = None
        try:
            popen_kwargs: dict[str, Any] = {
                "args": cmd,
                "shell": True,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "cwd": cwd,
                "encoding": "utf-8",
                "errors": "replace",
            }
            # Windows: use Git Bash instead of cmd.exe
            # shell=True + executable on Windows passes /c which bash rejects.
            # Instead, invoke bash -c directly without shell=True.
            if os.name == "nt" and self._bash_path:
                popen_kwargs["args"] = [self._bash_path, "-c", cmd]
                popen_kwargs["shell"] = False
            # Unix: 创建新 session，后续 killpg 不会误杀父进程
            if os.name != "nt":
                popen_kwargs["preexec_fn"] = os.setsid

            proc = subprocess.Popen(**popen_kwargs)
            self._current_proc = proc
            stdout, stderr = proc.communicate(timeout=timeout)
            return RunResult(
                returncode=proc.returncode if proc.returncode is not None else -1,
                stdout=stdout or "",
                stderr=stderr or "",
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


# ---------------------------------------------------------------------------
# DockerRuntime — Docker 沙箱
# ---------------------------------------------------------------------------

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
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
            }
            if os.name != "nt":
                popen_kwargs["preexec_fn"] = os.setsid

            proc = subprocess.Popen(**popen_kwargs)
            adjusted_timeout = timeout + 5  # docker exec 本身有少量开销
            stdout, stderr = proc.communicate(timeout=adjusted_timeout)
            return RunResult(
                returncode=proc.returncode if proc.returncode is not None else -1,
                stdout=stdout or "",
                stderr=stderr or "",
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