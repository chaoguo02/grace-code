"""
tools/test_tool.py

pytest 执行工具，返回结构化的测试结果。

关键设计：
- 不只返回原始 stdout，而是解析出"哪些测试失败了 + 错误信息"
- 失败时 output 包含精简的 failure summary，避免把整个 traceback 塞进上下文
- 通过 exit code 判断成功/失败，不依赖字符串匹配
- 对 pytest exit code 做确定性分类，避免把路径/参数错误误判成代码失败
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolResult
from tools.runtime import LocalRuntime, Runtime


PYTEST_TIMEOUT = 120        # pytest 默认超时，比 shell 工具更长
MAX_OUTPUT_CHARS = 50_000   # 测试输出可能很长（大 diff 等）


PYTEST_EXIT_MEANINGS = {
    0: "all tests passed",
    1: "tests failed",
    2: "pytest was interrupted",
    3: "pytest internal error",
    4: "pytest usage error",
    5: "no tests collected",
}


class PytestTool(BaseTool):
    """
    运行 pytest 并返回结构化结果。

    params:
        path (str):  测试文件或目录（默认 "tests/"，不存在则用 "."）
        args (str):  额外的 pytest 参数（如 "-x -v --tb=short"）
        cwd (str):   工作目录（默认当前目录）
    """

    def __init__(self, runtime: Runtime | None = None) -> None:
        self._runtime = runtime or LocalRuntime()

    @property
    def name(self) -> str:
        return "test"

    @property
    def description(self) -> str:
        return (
            "Run pytest and return a structured summary of results. "
            "Shows which tests failed and their error messages. "
            "Use path to run specific test files or directories."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Test file or directory to run (default: 'tests/' or '.')",
                },
                "args": {
                    "type": "string",
                    "description": "Extra pytest arguments (e.g. '-x -v --tb=short')",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory",
                },
            },
            "required": [],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        cwd = params.get("cwd", None)
        cwd_path = Path(cwd) if cwd else Path.cwd()

        # 决定测试路径
        test_path = params.get("path", "")
        if not test_path:
            if (cwd_path / "tests").exists():
                test_path = "tests/"
            else:
                test_path = "."

        extra_args = params.get("args", "")

        # ── Pre-flight: check pytest availability before execution ──
        import shutil as _shutil
        if not _shutil.which("pytest") and not _shutil.which("python"):
            from tools.base import ToolError as _ToolError
            return ToolResult(
                success=False, output="",
                error="pytest is not available in this environment.",
                tool_error=_ToolError(
                    error_type="unavailable", retryable=False,
                    detail="pytest is not installed or not on PATH. Verify changes by reading the modified files.",
                ),
            )

        # 组装命令：--tb=short 足够 agent 理解，--no-header 减少噪音
        args = [
            "-m", "pytest",
            test_path,
            "--tb=short",
            "--no-header",
            "-q",               # 安静模式：只输出失败详情和最终统计
        ]
        if extra_args:
            args.extend(extra_args.split())

        # Use parameterized execute() — shell=False, no string joining
        run_result = self._runtime.execute("python", args=args, cwd=cwd, timeout=PYTEST_TIMEOUT)

        # Use unified error classification for runtime-level failures
        if not run_result.success:
            from tools.base import classify_runtime_error
            cmd_repr = f"python -m pytest {test_path}"
            _runtime_err = classify_runtime_error(
                run_result.returncode, run_result.stderr, run_result.stdout, cmd_repr,
            )
            if _runtime_err and not _runtime_err.retryable:
                # Unavailable / permission denied → don't retry
                return ToolResult(
                    success=False, output="",
                    error=_runtime_err.to_message(), tool_error=_runtime_err,
                )
        raw = run_result.output
        success = run_result.returncode == 0

        output, error = _classify_pytest_result(run_result.returncode, raw, test_path, success)

        return ToolResult(
            success=success,
            output=output,
            error=error,
        )


# ---------------------------------------------------------------------------
# 结果分类与输出格式化
# ---------------------------------------------------------------------------

def _classify_pytest_result(
    returncode: int,
    raw: str,
    test_path: str,
    success: bool,
) -> tuple[str, str | None]:
    """按 pytest exit code 生成确定性摘要，减少 LLM 猜测空间。"""
    meaning = PYTEST_EXIT_MEANINGS.get(returncode, "unknown pytest exit code")

    if success:
        return _format_pytest_output(raw, success=True), None

    if returncode == 4:
        if _looks_like_missing_pytest_target(raw):
            return (
                "Pytest did not run because the requested test target is missing.\n"
                f"Requested path: {test_path}\n"
                "Classification: pytest exit code 4 (usage error / missing path).\n"
                "Do not create this missing test file unless the user explicitly asks for new tests.",
                "pytest usage error: requested test target is missing",
            )
        return (
            _join_nonempty(
                f"Classification: pytest exit code 4 ({meaning}).",
                "This is a command/path/argument problem, not an existing failing test.",
                _truncate_raw(raw),
            ),
            "pytest usage error",
        )

    if returncode == 5:
        return (
            _join_nonempty(
                f"Classification: pytest exit code 5 ({meaning}).",
                "No tests were collected. Do not create tests unless the user explicitly asks for new tests.",
                _truncate_raw(raw),
            ),
            "pytest collected no tests",
        )

    error = f"pytest exited with code {returncode} ({meaning})"
    return _format_pytest_output(raw, success=False), error


def _looks_like_missing_pytest_target(raw: str) -> bool:
    lower = raw.lower()
    patterns = (
        "file or directory not found",
        "not found:",
        "error: file or directory not found",
        "no such file or directory",
    )
    return any(pattern in lower for pattern in patterns)


def _truncate_raw(raw: str) -> str:
    if len(raw) > MAX_OUTPUT_CHARS:
        return "...[output truncated]...\n" + raw[-MAX_OUTPUT_CHARS:]
    return raw.strip()


def _join_nonempty(*parts: str) -> str:
    return "\n".join(part for part in parts if part)


# ---------------------------------------------------------------------------
# 输出格式化
# ---------------------------------------------------------------------------

def _format_pytest_output(raw: str, success: bool) -> str:
    """
    把 pytest 原始输出格式化为 agent 友好的摘要。

    成功时：返回通过统计行（如 "5 passed in 0.12s"）
    失败时：提取 FAILED 测试列表 + 每个失败的 short traceback
    """
    if len(raw) > MAX_OUTPUT_CHARS:
        # 失败时 agent 最需要看尾部（错误摘要），头部（收集信息）不重要
        raw = "...[output truncated]...\n" + raw[-MAX_OUTPUT_CHARS:]

    if success:
        # 只返回最后的统计行
        lines = raw.strip().splitlines()
        summary_lines = [l for l in lines if re.search(r"passed|no tests", l)]
        if summary_lines:
            return summary_lines[-1]
        return raw.strip()

    # 失败时：提取 FAILED 列表
    failed_lines = [l for l in raw.splitlines() if l.startswith("FAILED")]
    failed_section = "\n".join(failed_lines) if failed_lines else ""

    # 提取 short test summary info 之后的内容（pytest -q 会输出这块）
    short_summary_match = re.search(
        r"=+ short test summary info =+(.*?)(?:=+|\Z)",
        raw,
        re.DOTALL,
    )
    short_summary = short_summary_match.group(1).strip() if short_summary_match else ""

    # 最终统计行（如 "2 failed, 3 passed in 0.45s"）
    stat_match = re.search(r"\d+ (failed|error).*in \d+\.\d+s", raw)
    stat_line = stat_match.group(0) if stat_match else ""

    parts = []
    if failed_section:
        parts.append(f"Failed tests:\n{failed_section}")
    if short_summary and short_summary != failed_section:
        parts.append(f"Summary:\n{short_summary}")
    if stat_line:
        parts.append(stat_line)

    return "\n\n".join(parts) if parts else raw.strip()