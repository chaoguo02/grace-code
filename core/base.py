"""core/base.py

Core基础设施：
- ToolResult     工具执行结果
- BaseTool       所有工具的抽象基类
- ToolRegistry   工具注册表，core.py 通过它执行工具、生成 schema

新增工具只需：
    1. 继承 BaseTool，实现 execute() 和 schema 属性
    2. 调用 registry.register(MyTool())
    不需要改任何其他代码。
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import field, dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

# Re-export from core.types and core.errors for backward compatibility.
# New code should import directly from the type-specific modules.
logger = logging.getLogger(__name__)

from core.types import (
    Action,
    ActionType,
    LLMToolSchema,
    Observation,
    ObservationStatus,
    PathAccess,
    RiskLevel,
    ToolCall,
    ToolConcurrency,
    ToolDependency,
    ToolEffect,
    ToolMetadata,
    ToolOutcome,
    ToolRole,
)
from core.errors import (
    ToolError,
    ToolErrorType,
    ToolRetryDirective,
)


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
    structured_findings: tuple = ()     # 子代理的结构化发现（Finding dicts），用于自动记忆沉淀
    outcome: ToolOutcome = ToolOutcome.NONE
    metadata: dict[str, Any] = field(default_factory=dict)  # 工具返回的扩展元数据（如 skill contextModifier）
    modified_files: list[str] = field(default_factory=list)  # 此工具调用修改的文件路径列表

    def to_observation(self, tool_name: str) -> Observation:
        """转换为 Observation，供 core.py 写入 EventLog 和注入上下文。"""
        metadata: dict[str, Any] = {}
        if self.tool_error is not None:
            metadata["tool_error"] = {
                "error_type": self.tool_error.error_type.value,
                "retry": self.tool_error.retry.value,
                "alternative": self.tool_error.alternative,
            }
        return Observation(
            status=ObservationStatus.SUCCESS if self.success else ObservationStatus.ERROR,
            output=self.output,
            tool_name=tool_name,
            error=self.format_error_for_observation(),
            modified_files=list(self.modified_files),
            metadata=metadata,
            outcome=self.outcome,
        )

    def format_error_for_observation(self) -> str | None:
        """Build error message, preferring structured tool_error over raw string.

        Called from ``to_observation()`` — not private despite the former
        ``_format_error_for_observation`` name (P2-11)."""
        if self.tool_error is not None:
            return self.tool_error.to_message()
        return self.error

    @classmethod
    def from_error(
        cls,
        error_type: ToolErrorType,
        detail: str = "",
        *,
        retry: ToolRetryDirective = ToolRetryDirective.DO_NOT_RETRY,
        alternative: str = "",
    ) -> "ToolResult":
        """Factory: create a failed ToolResult with structured error."""
        return cls(
            success=False,
            output="",
            error=detail,
            tool_error=ToolError(
                error_type=error_type,
                retry=retry,
                alternative=alternative,
                detail=detail,
            ),
        )


# ---------------------------------------------------------------------------
# Runtime error classification — framework-level, not tool-specific
# ---------------------------------------------------------------------------

def classify_runtime_error(run_result: Any, cmd: str = "") -> ToolError | None:
    """Map Runtime-owned process facts to a typed tool failure.

    stderr/stdout remain presentation data. They are deliberately excluded
    from classification so diagnostic wording cannot change control flow.
    """
    from core.process import ProcessTermination

    if run_result.success:
        return None

    cmd_name = cmd.split()[0] if cmd.strip() else "command"

    if run_result.termination is ProcessTermination.TIMED_OUT:
        return ToolError(
            error_type=ToolErrorType.TIMEOUT,
            retry=ToolRetryDirective.RETRY,
            detail=f"Command timed out: {cmd[:80]!r}",
        )

    if run_result.termination is ProcessTermination.INTERRUPTED:
        return ToolError(
            error_type=ToolErrorType.INTERRUPTED,
            detail=f"Command interrupted: {cmd[:80]!r}",
        )

    if (
        run_result.termination is ProcessTermination.START_FAILED
        or run_result.returncode in (127, 9009)
    ):
        return ToolError(
            error_type=ToolErrorType.ENVIRONMENT_UNAVAILABLE,
            detail=f"Runtime could not start {cmd_name!r}. {run_result.stderr.strip()[:200]}",
            alternative=f"Provide a project-local or Runtime-injected {cmd_name!r} executable.",
        )

    return ToolError(
        error_type=ToolErrorType.PROCESS_FAILED,
        retry=ToolRetryDirective.RETRY,
        detail=(
            f"Exit code {run_result.returncode}: "
            f"{run_result.stderr.strip()[:200] or run_result.stdout.strip()[:200]}"
        ),
    )


# ---------------------------------------------------------------------------
# BaseTool
# ---------------------------------------------------------------------------
# ExecutionContext — unified environment passed to every tool invocation
# ---------------------------------------------------------------------------

@dataclass
class ExecutionContext:
    """Environment context available to every tool at execution time.

    Tools destructure what they need from this object. No more
    requires_workspace / requires_git_root flags on BaseTool.
    """
    workspace_root: str = ""
    repo_path: str = ""


@runtime_checkable
class WorkspaceAware(Protocol):
    """Protocol: tools that accept a workspace_root for path resolution.

    Use isinstance(tool, WorkspaceAware) instead of hasattr(tool, '_workspace_root').
    This is type-safe — static checkers verify the attribute exists.
    """
    _workspace_root: str


@runtime_checkable
class ScopableRuntime(Protocol):
    def scoped(self, workspace_root: str) -> Any:
        ...


@runtime_checkable
class ProjectScopablePermissionPipeline(Protocol):
    """Permission pipeline that can bind its path sandbox to a child project root."""

    def scoped(self, project_root: str) -> Any:
        ...


@runtime_checkable
class AgentScopablePermissionPipeline(Protocol):
    """Permission pipeline that can identify a requesting child agent."""

    def for_agent(self, agent_name: str) -> Any:
        ...


@runtime_checkable
class RuntimeBoundTool(Protocol):
    _runtime: Any


@runtime_checkable
class RunContextAware(Protocol):
    """Protocol for tools that consume typed, per-run Runtime resources."""

    def with_run_context(self, context: Any) -> "BaseTool":
        ...


# ---------------------------------------------------------------------------

class BaseTool(ABC):
    """
    所有工具的抽象基类。

    子类必须实现：
    - name:     工具名称（与 LLM function calling 的函数名对应）
    - schema:   JSON Schema 描述，告诉 LLM 这个工具怎么用
    - execute(): 实际执行逻辑
    """

    aliases: tuple[str, ...] = ()
    """Alternative names the LLM might use (Claude Code conventions)."""

    _registry: Any = None
    """Injected by ToolRegistry.register() — enables signal tools to set
    mode-switch flags on the registry for the main loop to pick up."""

    metadata = ToolMetadata()

    def bind_context(self, context: ExecutionContext) -> "BaseTool":
        """Clone this tool and inject one session's immutable project scope."""
        bound = copy.copy(self)
        if isinstance(bound, WorkspaceAware):
            bound._workspace_root = context.workspace_root
        if isinstance(bound, RuntimeBoundTool):
            if ToolRole.DELEGATE in bound.metadata.roles:
                return bound
            if not isinstance(bound._runtime, ScopableRuntime):
                raise ValueError(
                    f"Tool {bound.name!r} runtime cannot bind workspace context"
                )
            bound._runtime = bound._runtime.scoped(context.workspace_root)
        return bound

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

    def classify_risk(self, params: dict[str, Any]) -> str:
        """
        动态风险分类。根据参数决定实际风险等级。
        默认返回 self.risk_level。ShellTool 覆写此方法实现命令级分类。
        """
        return self.risk_level

    def permission_denial_reason(self, params: dict[str, Any]) -> str | None:
        """Return a Runtime safety denial reason, or ``None`` when valid."""
        return None

    def concurrency_mode(self, params: dict[str, Any]) -> ToolConcurrency:
        """Declare whether this specific call may run beside sibling calls."""
        return ToolConcurrency.SERIAL

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

    def __init__(
        self,
        hitl_manager: Any = None,
        permission_pipeline: Any = None,
        hook_dispatcher: Any = None,
        capability_registry: Any = None,
    ) -> None:
        """Create a tool registry with optional Runtime-owned intercept layers.

        All parameters use ``Any`` at runtime to avoid circular imports from
        hitl/hooks packages (P2-10).
        """
        self._tools: dict[str, BaseTool] = {}
        self._tool_aliases: dict[str, str] = {}
        self._permission_pipeline = permission_pipeline
        self._hitl_manager = hitl_manager
        self._hook_dispatcher = hook_dispatcher
        self._capability_registry = capability_registry
        self._stats_lock = threading.Lock()
        """Protects ``_timing_stats`` — multiple threads call ``execute_tool``
        concurrently in Web mode (ACC-4a)."""
        self._timing_stats: dict[str, dict[str, float | int]] = {}

    def register(self, tool: BaseTool) -> "ToolRegistry":
        """
        注册一个工具。支持链式调用：
            registry.register(ShellTool()).register(FileTool())
        """
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool
        # Inject registry reference so signal tools can set mode-switch flags
        tool._registry = self
        # Register aliases (tool naming aligned with LLM prior knowledge)
        for alias in getattr(tool, "aliases", ()):
            if alias in self._tool_aliases:
                logger.warning("Tool alias '%s' → '%s' shadowed by existing alias → '%s'",
                               alias, tool.name, self._tool_aliases[alias])
            self._tool_aliases[alias] = tool.name
        return self

    def resolve_name(self, name: str) -> str | None:
        """Resolve a possibly-aliased tool name to its canonical name.

        Returns the canonical name if the tool exists (directly or via alias),
        or None if the tool is completely unknown.
        """
        if name in self._tools:
            return name
        return self._tool_aliases.get(name)

    def metadata_for(self, name: str) -> ToolMetadata | None:
        """Return metadata for a canonical or aliased registered tool."""
        canonical = self.resolve_name(name)
        if canonical is None:
            return None
        metadata = getattr(self._tools[canonical], "metadata", None)
        return metadata if isinstance(metadata, ToolMetadata) else ToolMetadata()

    def concurrency_for(
        self, name: str, params: dict[str, Any],
    ) -> ToolConcurrency:
        """Return a call-specific scheduling fact; unknown calls fail closed."""
        canonical = self.resolve_name(name)
        if canonical is None:
            return ToolConcurrency.SERIAL
        return self._tools[canonical].concurrency_mode(params)

    def execute_tool(self, name: str, params: dict[str, Any], thought: str = "") -> ToolResult:
        """
        按名称查找工具并执行。
        如果有 HitlManager，先经过 HITL 审批。
        工具不存在时返回 error ToolResult（不抛异常，让 agent 继续运行）。
        """
        start = time.perf_counter()
        result: ToolResult

        # Resolve aliases — LLM may use Claude Code naming conventions
        canonical = self.resolve_name(name)
        if canonical is None:
            available = ", ".join(self._tools.keys()) or "none"
            result = ToolResult.from_error(
                error_type=ToolErrorType.NOT_FOUND,
                detail=f"Unknown tool '{name}'. Available tools: {available}",
            )
            self._record_timing(name, start, result)
            return result

        tool = self._tools[canonical]

        # Runtime-owned capability facts physically remove unavailable tools.
        if self._capability_registry is not None:
            import json as _json
            from agent.capability_registry import InterceptDecision
            intercept = self._capability_registry.intercept(
                canonical, session_id=getattr(self, "_session_id", ""),
            )
            if intercept.decision is InterceptDecision.BLOCK:
                feedback_json = _json.dumps(intercept.feedback, ensure_ascii=False)
                result = ToolResult.from_error(
                    error_type=ToolErrorType.UNAVAILABLE,
                    detail=f"Tool '{name}' blocked: {feedback_json}",
                )
                self._record_timing(name, start, result)
                return result

        perm_result = None
        # Permission Pipeline gate (5-layer evaluation)
        if self._permission_pipeline is not None:
            perm_result = self._permission_pipeline.check(tool, params, thought=thought)
            from hitl.pipeline import PermissionDecision
            if perm_result.decision is PermissionDecision.DENY:
                feedback = getattr(perm_result, "feedback", "")
                error_msg = f"Tool '{name}' denied: {perm_result.reason}"
                if feedback:
                    error_msg += f" Feedback: {feedback}"
                result = ToolResult.from_error(
                    error_type=ToolErrorType.PERMISSION_DENIED,
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
                    error_type=ToolErrorType.PERMISSION_DENIED,
                    detail=error_msg,
                )
                self._record_timing(name, start, result)
                return result

        # Apply updatedInput from PreToolUse hooks (CC-aligned)
        actual_params = params
        if perm_result is not None and getattr(perm_result, "updated_params", None):
            actual_params = {**params, **perm_result.updated_params}

        try:
            result = tool.execute(actual_params)
        except Exception as exc:
            # 工具内部未捕获的异常，降级为 error 结果
            result = ToolResult.from_error(
                error_type=ToolErrorType.INTERNAL,
                detail=f"Tool '{name}' raised an unexpected error: {exc}",
            )

        # Fire PostToolUse / PostToolUseFailure hook (CC-aligned)
        if self._hook_dispatcher:
            _post_result = self._fire_post_tool_hook(name, actual_params, result)
            if _post_result is not None:
                # Apply additionalContext from PostToolUse hook
                if getattr(_post_result, "additional_context", ""):
                    result.output = result.output + "\n\n[Hook context]\n" + _post_result.additional_context

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
        # Preserve aliases for filtered tools — critical for LLM tool name
        # compatibility (e.g. "file_read" → "Read", "search_text" → "Grep")
        for alias, canonical in self._tool_aliases.items():
            if canonical in filtered._tools:
                filtered._tool_aliases[alias] = canonical
        return filtered

    def excluding_roles(self, roles: frozenset[ToolRole]) -> "ToolRegistry":
        """Return a registry without tools owning any prohibited protocol role."""
        return self.filtered(frozenset(
            name
            for name in self.tool_names
            if not (self.metadata_for(name).roles & roles)
        ))

    def with_permission_request_origin(self, agent_name: str) -> "ToolRegistry":
        """Clone registry policy and identify its child permission requester."""
        derived = copy.copy(self)
        derived._timing_stats = {}
        pipeline = self._permission_pipeline
        if isinstance(pipeline, AgentScopablePermissionPipeline):
            derived._permission_pipeline = pipeline.for_agent(agent_name)
        return derived

    def scoped(self, context: ExecutionContext) -> "ToolRegistry":
        """Clone registered tools into an isolated per-session context."""
        permission_pipeline = self._permission_pipeline
        if isinstance(permission_pipeline, ProjectScopablePermissionPipeline):
            permission_pipeline = permission_pipeline.scoped(
                context.repo_path or context.workspace_root
            )
        scoped = ToolRegistry(
            hitl_manager=self._hitl_manager,
            permission_pipeline=permission_pipeline,
            hook_dispatcher=self._hook_dispatcher,
            capability_registry=self._capability_registry,
        )
        for tool in self._tools.values():
            scoped.register(tool.bind_context(context))
        return scoped

    def with_run_context(self, context: Any) -> "ToolRegistry":
        """Clone only tools that declaratively consume per-run resources."""
        # Preserve registry-level dependency references and session metadata;
        # only tool instances and per-run counters belong to the new binding.
        bound = copy.copy(self)
        bound._tools = {}
        bound._tool_aliases = {}
        bound._timing_stats = {}
        for tool in self._tools.values():
            bound.register(
                tool.with_run_context(context)
                if isinstance(tool, RunContextAware)
                else tool
            )
        return bound

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __repr__(self) -> str:
        return f"ToolRegistry(tools={self.tool_names})"

    def _fire_post_tool_hook(self, name: str, params: dict[str, Any], result: ToolResult) -> Any:
        """Fire PostToolUse or PostToolUseFailure via dispatcher. Returns DispatchResult."""
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
            return self._hook_dispatcher.dispatch(evt, ctx)
        except Exception:
            pass
        return None

    def _record_timing(self, name: str, start: float, result: ToolResult) -> None:
        elapsed_ms = (time.perf_counter() - start) * 1000
        result.duration_ms = elapsed_ms
        with self._stats_lock:
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
        with self._stats_lock:
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
        with self._stats_lock:
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
