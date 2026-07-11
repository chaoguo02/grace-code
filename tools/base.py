"""
tools/base.py

工具层基础设施：
- ToolResult     工具执行结果
- BaseTool       所有工具的抽象基类
- ToolRegistry   工具注册表，core.py 通过它执行工具、生成 schema

新增工具只需：
    1. 继承 BaseTool，实现 execute() 和 schema 属性
    2. 调用 registry.register(MyTool())
    不需要改任何其他代码。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any

from agent.task import Observation, ObservationStatus
from llm.base import LLMToolSchema


# ---------------------------------------------------------------------------
# RiskLevel — 工具风险分级
# ---------------------------------------------------------------------------

class RiskLevel(str, Enum):
    """工具风险等级。HitlManager 根据此决定是否需要人工确认。"""
    NONE = "none"       # file_read, git_status — 只读，永不提示
    LOW = "low"         # git_add, memory_write — 可逆，通常跳过
    MEDIUM = "medium"   # file_write — 覆盖文件，可配置
    HIGH = "high"       # shell(dangerous), git_commit — 不可逆，总是提示


# ---------------------------------------------------------------------------
# ToolError — structured tool error information
# ---------------------------------------------------------------------------

@dataclass
class ToolError:
    """Structured error from tool execution.

    Unlike raw string errors, this gives the Runtime and LLM enough
    information to decide: should I retry? Is there an alternative tool?

    error_type values:
        timeout          — operation exceeded time limit (retryable)
        permission_denied — HITL/permission layer blocked (not retryable)
        not_found        — file/path/resource doesn't exist (retryable with correction)
        internal         — unexpected internal error (not retryable)
        rate_limited     — API rate limit hit (retryable after backoff)
        invalid_params   — tool called with wrong parameters (retryable with correction)
        unavailable      — tool is permanently unavailable (not retryable, use alternative)
        circuit_open     — tool's circuit breaker is open (retryable after backoff)
    """

    error_type: str       # see docstring for values
    retryable: bool       # can the LLM retry with different params?
    alternative: str = "" # suggested alternative tool name, e.g. "shell" for "bash"
    detail: str = ""      # human-readable detail for the LLM

    def to_message(self) -> str:
        """Format as a single line for LLM context injection."""
        parts = [f"[{self.error_type}]"]
        if self.detail:
            parts.append(f" {self.detail}")
        if self.retryable:
            parts.append(" (retryable)")
        if self.alternative:
            parts.append(f" (try '{self.alternative}' instead)")
        return "".join(parts)


# ---------------------------------------------------------------------------
# ToolResult
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """
    工具执行的原始结果，由各 Tool.execute() 返回。
    core.py 把它转换为 Observation 后写入 EventLog。
    """
    success: bool
    output: str                         # 工具的文本输出，已做截断处理
    error: str | None = None            # 失败时的错误信息（向后兼容，建议使用 tool_error）
    tool_error: ToolError | None = None # 结构化错误信息（新增，Runtime可据此决策）
    duration_ms: float = 0.0            # 工具执行耗时（毫秒），由 ToolRegistry 填充
    cached: bool = False                # True = 结果来自缓存命中（无实际 I/O）
    subagent_tokens_used: int = 0       # 子代理消耗的 token 数，父代理预算需计入
    subagent_terminated_by_loop: bool = False  # 子代理被循环检测终止
    structured_findings: tuple = ()     # 子代理的结构化发现（Finding dicts），用于自动记忆沉淀

    def to_observation(self, tool_name: str) -> Observation:
        """转换为 Observation，供 core.py 写入 EventLog 和注入上下文。"""
        metadata: dict[str, Any] = {}
        if self.tool_error is not None:
            metadata["tool_error"] = {
                "error_type": self.tool_error.error_type,
                "retryable": self.tool_error.retryable,
                "alternative": self.tool_error.alternative,
            }
        return Observation(
            status=ObservationStatus.SUCCESS if self.success else ObservationStatus.ERROR,
            output=self.output,
            tool_name=tool_name,
            error=self._format_error_for_observation(),
            metadata=metadata,
        )

    def _format_error_for_observation(self) -> str | None:
        """Build error message, preferring structured tool_error over raw string."""
        if self.tool_error is not None:
            return self.tool_error.to_message()
        return self.error

    @classmethod
    def from_error(
        cls,
        error_type: str,
        detail: str = "",
        *,
        retryable: bool = False,
        alternative: str = "",
    ) -> "ToolResult":
        """Factory: create a failed ToolResult with structured error."""
        return cls(
            success=False,
            output="",
            error=detail,
            tool_error=ToolError(
                error_type=error_type,
                retryable=retryable,
                alternative=alternative,
                detail=detail,
            ),
        )


# ---------------------------------------------------------------------------
# BaseTool
# ---------------------------------------------------------------------------

class BaseTool(ABC):
    """
    所有工具的抽象基类。

    子类必须实现：
    - name:     工具名称（与 LLM function calling 的函数名对应）
    - schema:   JSON Schema 描述，告诉 LLM 这个工具怎么用
    - execute(): 实际执行逻辑
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称，如 "shell", "file_read"。必须全局唯一。"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """工具功能描述，注入 LLM 的 system prompt 和 tool schema。"""
        ...

    @property
    @abstractmethod
    def parameters_schema(self) -> dict[str, Any]:
        """
        参数的 JSON Schema。示例：
        {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "Shell command to run"},
            },
            "required": ["cmd"],
        }
        """
        ...

    @property
    def risk_level(self) -> str:
        """静态风险等级。子类可覆写。默认 NONE（只读工具）。"""
        return RiskLevel.NONE

    # ── Semantic properties (Claude Code style) ──
    # Tool Gateway filters on these. Fail-closed: defaults are conservative.

    @property
    def is_read_only(self) -> bool:
        """True if this tool never modifies filesystem or external state.

        Read-only tools are automatically available to plan/explore agents.
        Default False (fail-closed): a tool that forgets to declare this
        is treated as potentially destructive.
        """
        return False

    @property
    def allows_delegation(self) -> bool:
        """True if this tool spawns sub-agents (e.g. task tool).

        Delegation tools require explicit opt-in via TaskContract.allowed_actions.
        Default False: a tool cannot spawn sub-agents unless declared.
        """
        return False

    def classify_risk(self, params: dict[str, Any]) -> str:
        """
        动态风险分类。根据参数决定实际风险等级。
        默认返回 self.risk_level。ShellTool 覆写此方法实现命令级分类。
        """
        return self.risk_level

    @abstractmethod
    def execute(self, params: dict[str, Any]) -> ToolResult:
        """执行工具，返回 ToolResult。不抛异常——所有异常已在内部处理。"""
        ...

    def to_llm_schema(self) -> LLMToolSchema:
        """生成供 LLM 使用的 schema，由 ToolRegistry 调用。"""
        return LLMToolSchema(
            name=self.name,
            description=self.description,
            parameters=self.parameters_schema,
        )


# ---------------------------------------------------------------------------
# Path safety — hard security boundary for file tools
# ---------------------------------------------------------------------------
# Defense in Depth (three layers):
#   1. sanitize_path()  — string-level ../ removal (Sanitizer)
#   2. is_path_safe()   — parent directory resolution check
#   3. safe_open_for_write() — platform-adaptive atomic open (TOCTOU protection)
#
# On POSIX: uses O_NOFOLLOW to atomically reject symlinks
# On Windows: checks is_symlink() before open (no kernel-level symlink TOCTOU on Win)

import errno as _errno
import os as _os
import sys as _sys
from pathlib import Path as _Path

# Platform-adaptive: O_NOFOLLOW is POSIX-only
_O_NOFOLLOW = getattr(_os, "O_NOFOLLOW", 0)


def sanitize_path(user_path: str, workspace_root: str) -> str:
    """Clean user-supplied path: resolve ../, ensure within workspace.

    Layer 1 (Sanitizer): string-level path normalization. Runs BEFORE
    any file operation. Strips ../ traversal attempts without touching
    the filesystem.
    """
    if _os.path.isabs(user_path):
        clean = _os.path.normpath(user_path)
    else:
        clean = _os.path.normpath(_os.path.join(workspace_root, user_path))

    ws = _os.path.normpath(workspace_root)
    if not clean.startswith(ws):
        raise ValueError(
            f"Path '{user_path}' resolves to '{clean}' which escapes "
            f"workspace '{workspace_root}'"
        )
    return clean


def is_path_safe(target: str, workspace_root: str) -> bool:
    """Check that target path (resolved, symlinks followed) is within workspace.

    Layer 2: filesystem-level boundary check. Resolves symlinks on the
    full path. Use this for reading existing files. For writing, use
    resolve_safe_parent() + O_NOFOLLOW to prevent TOCTOU.
    """
    try:
        target_path = _Path(target).resolve()
        root_path = _Path(workspace_root).resolve()
        target_path.relative_to(root_path)
        return True
    except (ValueError, OSError):
        return False


def resolve_safe_parent(target: str, workspace_root: str) -> tuple[str, str] | tuple[None, str]:
    """Resolve parent directory and return (safe_full_path, error).

    Layer 3 preparation for TOCTOU-safe writes:
      1. Sanitize the path string
      2. Resolve the PARENT directory (follows symlinks on dirs)
      3. Check resolved parent is within workspace
      4. Return (parent/target_name, "") — caller opens with O_NOFOLLOW

    Does NOT follow symlinks on the final path component — that's the
    caller's job via O_NOFOLLOW.
    """
    # 1. Sanitize
    try:
        clean = sanitize_path(target, workspace_root)
    except ValueError as e:
        return None, str(e)

    p = _Path(clean)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return None, f"Cannot create parent directory: {e}"

    # 2. Resolve parent (follows symlinks on directory components)
    try:
        parent_resolved = p.parent.resolve()
    except OSError as e:
        return None, f"Cannot resolve parent directory: {e}"

    # 3. Check parent within workspace
    try:
        ws = _Path(workspace_root).resolve()
        parent_resolved.relative_to(ws)
    except ValueError:
        return None, (
            f"Parent directory '{parent_resolved}' is outside "
            f"workspace '{ws}'"
        )

    full = str(parent_resolved / p.name)
    return full, ""


def safe_open_for_write(full_path: str) -> tuple[int | None, str]:
    """Open a file for writing with TOCTOU protection. Returns (fd, error).

    On POSIX: uses O_NOFOLLOW — kernel rejects symlinks atomically.
    On Windows: checks is_symlink() before open (no kernel symlink TOCTOU).
    """
    p = _Path(full_path)
    # Windows: explicit symlink check (O_NOFOLLOW is not available)
    if _sys.platform == "win32" and p.exists() and p.is_symlink():
        return None, f"Cannot write to symlink: {full_path}"
    flags = _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC
    if _O_NOFOLLOW:
        flags |= _O_NOFOLLOW
    try:
        fd = _os.open(full_path, flags)
        return fd, ""
    except OSError as e:
        return None, f"Cannot open for write '{full_path}': {e}"


def safe_create_file(full_path: str) -> tuple[int | None, str]:
    """Create a NEW file with TOCTOU protection. Returns (fd, error).
    Fails if the file already exists (O_EXCL)."""
    p = _Path(full_path)
    if _sys.platform == "win32" and p.exists():
        return None, f"File already exists: {full_path}"
    flags = _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL
    if _O_NOFOLLOW:
        flags |= _O_NOFOLLOW
    try:
        fd = _os.open(full_path, flags)
        return fd, ""
    except OSError as e:
        return None, f"Cannot create '{full_path}': {e}"


def safe_read_text(target: str, workspace_root: str) -> tuple[str | None, str]:
    """Read file content with path safety check. Returns (content, error)."""
    if not is_path_safe(target, workspace_root):
        return None, f"Path '{target}' is outside workspace"
    try:
        return _Path(target).read_text(encoding="utf-8", errors="replace"), ""
    except OSError as e:
        return None, str(e)


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """
    工具注册表。core.py 持有一个 registry 实例，通过它：
    1. 查找工具并执行（execute_tool）
    2. 生成所有工具的 schema 列表注入 LLM（get_schemas）
    3. 记录每个工具的执行耗时统计（get_timing_stats）
    """

    def __init__(self, hitl_manager: Any = None, permission_pipeline: Any = None, hook_dispatcher: Any = None, capability_registry: Any = None) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._permission_pipeline = permission_pipeline
        # Backward compat: hitl_manager still accepted, pipeline takes precedence
        self._hitl_manager = hitl_manager
        self._hook_dispatcher = hook_dispatcher
        self._capability_registry = capability_registry  # P1-6: dynamic capability check
        self._timing_stats: dict[str, dict[str, float | int]] = {}

    def register(self, tool: BaseTool) -> "ToolRegistry":
        """
        注册一个工具。支持链式调用：
            registry.register(ShellTool()).register(FileTool())
        """
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool
        return self

    def execute_tool(self, name: str, params: dict[str, Any], thought: str = "") -> ToolResult:
        """
        按名称查找工具并执行。
        如果有 HitlManager，先经过 HITL 审批。
        工具不存在时返回 error ToolResult（不抛异常，让 agent 继续运行）。
        """
        start = time.perf_counter()
        result: ToolResult

        if name not in self._tools:
            available = ", ".join(self._tools.keys()) or "none"
            result = ToolResult.from_error(
                error_type="not_found",
                detail=f"Unknown tool '{name}'. Available tools: {available}",
            )
            self._record_timing(name, start, result)
            return result

        tool = self._tools[name]

        # ── P1-6: Physical interception with dedup (structured feedback) ──
        if self._capability_registry is not None:
            import json as _json
            from agent.capability_registry import InterceptHardBlock
            try:
                intercept = self._capability_registry.intercept(
                    name, session_id=getattr(self, "_session_id", ""),
                )
                if intercept.blocked:
                    feedback_json = _json.dumps(intercept.feedback, ensure_ascii=False)
                    result = ToolResult.from_error(
                        error_type="unavailable",
                        detail=f"Tool '{name}' blocked: {feedback_json}",
                    )
                    self._record_timing(name, start, result)
                    return result
            except InterceptHardBlock:
                # Propagate to main loop — this session cannot continue
                raise

        # Permission Pipeline gate (5-layer evaluation)
        if self._permission_pipeline is not None:
            perm_result = self._permission_pipeline.check(tool, params, thought=thought)
            if not perm_result.approved:
                feedback = getattr(perm_result, "feedback", "")
                error_msg = f"Tool '{name}' denied: {perm_result.reason}"
                if feedback:
                    error_msg += f" Feedback: {feedback}"
                result = ToolResult.from_error(
                    error_type="permission_denied",
                    detail=error_msg,
                )
                self._record_timing(name, start, result)
                return result
        # Legacy HITL gate (backward compat when no pipeline)
        elif self._hitl_manager is not None:
            hitl_result = self._hitl_manager.check(tool, params, thought=thought)
            if hitl_result.is_denied:
                note = hitl_result.feedback_note
                error_msg = f"Tool '{name}' denied by user."
                if note:
                    error_msg += f" Feedback: {note}"
                result = ToolResult.from_error(
                    error_type="permission_denied",
                    detail=error_msg,
                )
                self._record_timing(name, start, result)
                return result

        try:
            result = tool.execute(params)
        except Exception as exc:
            # 工具内部未捕获的异常，降级为 error 结果
            result = ToolResult.from_error(
                error_type="internal",
                detail=f"Tool '{name}' raised an unexpected error: {exc}",
            )

        # Fire PostToolUse / PostToolUseFailure hook
        if self._hook_dispatcher:
            self._fire_post_tool_hook(name, params, result)

        self._record_timing(name, start, result)
        return result

    def get_schemas(self) -> list[LLMToolSchema]:
        """返回所有已注册工具的 schema（按 name 排序，确保 prompt caching 稳定性）。"""
        schemas = [tool.to_llm_schema() for tool in self._tools.values()]
        schemas.sort(key=lambda s: s.name)
        return schemas

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    def filtered(self, allowed_tools: set[str] | frozenset[str]) -> "ToolRegistry":
        """返回只包含指定工具的新注册表，保留所有拦截层（pipeline, HITL, hooks, capability）。"""
        filtered = ToolRegistry(
            hitl_manager=self._hitl_manager,
            permission_pipeline=self._permission_pipeline,
            hook_dispatcher=self._hook_dispatcher,
            capability_registry=self._capability_registry,
        )
        for tool_name in self.tool_names:
            if tool_name in allowed_tools:
                filtered._tools[tool_name] = self._tools[tool_name]
        return filtered

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __repr__(self) -> str:
        return f"ToolRegistry(tools={self.tool_names})"

    def _fire_post_tool_hook(self, name: str, params: dict[str, Any], result: ToolResult) -> None:
        """Fire PostToolUse or PostToolUseFailure event via the hook dispatcher."""
        from hooks.events import HookContext, HookEvent

        evt = HookEvent.POST_TOOL_USE if result.success else HookEvent.POST_TOOL_USE_FAILURE
        ctx = HookContext(
            event=evt,
            tool_name=name,
            tool_input=params,
            tool_output={
                "success": result.success,
                "output": result.output[:2000],
                "error": result.error or "",
            },
        )
        try:
            self._hook_dispatcher.dispatch(evt, ctx)
        except Exception:
            pass

    def _record_timing(self, name: str, start: float, result: ToolResult) -> None:
        elapsed_ms = (time.perf_counter() - start) * 1000
        result.duration_ms = elapsed_ms
        stats = self._timing_stats.setdefault(
            name,
            {
                "calls": 0,
                "failures": 0,
                "total_duration_ms": 0.0,
                "min_duration_ms": 0.0,
                "max_duration_ms": 0.0,
            },
        )
        calls = int(stats["calls"])
        stats["calls"] = calls + 1
        stats["failures"] = int(stats["failures"]) + (0 if result.success else 1)
        stats["total_duration_ms"] = float(stats["total_duration_ms"]) + elapsed_ms
        stats["min_duration_ms"] = elapsed_ms if calls == 0 else min(float(stats["min_duration_ms"]), elapsed_ms)
        stats["max_duration_ms"] = elapsed_ms if calls == 0 else max(float(stats["max_duration_ms"]), elapsed_ms)

    # ── 统计接口 ──────────────────────────────────────────────────────

    def get_timing_stats(self) -> dict[str, dict[str, float | int]]:
        """
        返回工具执行耗时统计快照。
        格式：{tool_name: {calls, failures, total/avg/min/max_duration_ms}}
        """
        snapshot: dict[str, dict[str, float | int]] = {}
        for name, stats in self._timing_stats.items():
            calls = int(stats["calls"])
            total = float(stats["total_duration_ms"])
            snapshot[name] = {
                "calls": calls,
                "failures": int(stats["failures"]),
                "total_duration_ms": total,
                "avg_duration_ms": total / calls if calls else 0.0,
                "min_duration_ms": float(stats["min_duration_ms"]),
                "max_duration_ms": float(stats["max_duration_ms"]),
            }
        return snapshot

    def reset_timing_stats(self) -> None:
        """清空所有工具执行耗时统计。"""
        self._timing_stats.clear()


# ---------------------------------------------------------------------------
# NoopTool — 测试辅助
# ---------------------------------------------------------------------------

class NoopTool(BaseTool):
    """
    测试专用工具，execute() 直接返回成功，不做任何实际操作。
    用于在不依赖真实文件系统/shell 的情况下测试 core.py 流程。
    """

    def __init__(self, tool_name: str = "noop", output: str = "ok") -> None:
        self._name = tool_name
        self._output = output
        self.call_count = 0
        self.last_params: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"No-op tool '{self._name}' for testing."

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Anything"},
            },
            "required": [],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        self.call_count += 1
        self.last_params = params
        return ToolResult(success=True, output=self._output)


class FailingTool(BaseTool):
    """
    测试专用工具，execute() 始终返回失败。
    用于测试 Reflection 触发（测试失败路径）。
    """

    def __init__(self, tool_name: str = "test", error_msg: str = "AssertionError: 1 != 2") -> None:
        self._name = tool_name
        self._error_msg = error_msg
        self.call_count = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Always-failing tool '{self._name}' for testing."

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, params: dict[str, Any]) -> ToolResult:
        self.call_count += 1
        return ToolResult(
            success=False,
            output=self._error_msg,
            error=self._error_msg,
        )
