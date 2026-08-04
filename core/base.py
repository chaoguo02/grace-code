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
from collections.abc import Iterable
from dataclasses import field, dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from hooks.protocol import HookAttachment

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
    TOOL_SOURCE_PRIORITY,
    RetryMode,
    RetryPolicy,
    IdempotencyStrategy,
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

    **Field categorization** (Phase 3 #8):

    | Category | Fields | Consumer |
    |----------|--------|----------|
    | **Output payload** | ``output``, ``error``, ``tool_error`` | Observation rendering → model context |
    | **Action evidence** | ``modified_files``, ``outcome``, ``attachments`` | CompletionGuard, evidence chain |
    | **Runtime metadata** | ``success``, ``duration_ms``, ``cached``, ``subagent_tokens_used``, ``structured_findings``, ``metadata``, ``data``, ``invocation_id``, ``attempt_count``, ``eventual_success`` | Agent loop internals, budget, memory |

    Output payload fields are rendered to the model. Action evidence
    fields drive completion evaluation and evidence recording. Runtime
    metadata fields are consumed by the agent loop and never sent to
    the LLM directly.
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
    data: Any | None = None                # Optional typed/raw result payload; output remains compatibility rendering.
    attachments: tuple["HookAttachment", ...] = ()
    invocation_id: str = ""
    attempt_count: int = 1
    eventual_success: bool = False

    def normalized_outcome(self) -> ToolOutcome:
        """Return the stable outcome vocabulary for this result."""
        if self.outcome is not ToolOutcome.NONE:
            return self.outcome
        if self.tool_error is not None:
            return _outcome_for_error_type(self.tool_error.error_type)
        if not self.success:
            return ToolOutcome.FAILED
        if not self.output.strip():
            return ToolOutcome.EMPTY
        return ToolOutcome.NONE

    def to_observation(self, tool_name: str) -> Observation:
        """转换为 Observation，供 core.py 写入 EventLog 和注入上下文。"""
        metadata: dict[str, Any] = {}
        if self.tool_error is not None:
            metadata["tool_error"] = {
                "error_type": self.tool_error.error_type.value,
                "retry": self.tool_error.retry.value,
                "alternative": self.tool_error.alternative,
            }
        evidence_ref = self.metadata.get("evidence_ref")
        if isinstance(evidence_ref, dict):
            metadata["evidence_ref"] = dict(evidence_ref)
        return Observation(
            status=ObservationStatus.SUCCESS if self.success else ObservationStatus.ERROR,
            output=self.output,
            tool_name=tool_name,
            error=self.format_error_for_observation(),
            modified_files=list(self.modified_files),
            metadata=metadata,
            outcome=self.outcome if self.outcome is not ToolOutcome.NONE else _derive_tool_outcome(self),
            attachments=self.attachments,
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
            outcome=_outcome_for_error_type(error_type),
        )


# ---------------------------------------------------------------------------
# Runtime error classification — framework-level, not tool-specific
# ---------------------------------------------------------------------------

def _outcome_for_error_type(error_type: ToolErrorType) -> ToolOutcome:
    if error_type in {
        ToolErrorType.PERMISSION_DENIED,
        ToolErrorType.UNAVAILABLE,
        ToolErrorType.INVALID_PARAMS,
    }:
        return ToolOutcome.BLOCKED
    if error_type is ToolErrorType.TIMEOUT:
        return ToolOutcome.FAILED
    if error_type in {
        ToolErrorType.INTERRUPTED,
    }:
        return ToolOutcome.SKIPPED
    return ToolOutcome.FAILED


def _derive_tool_outcome(result: "ToolResult") -> ToolOutcome:
    if result.tool_error is not None:
        return _outcome_for_error_type(result.tool_error.error_type)
    if result.outcome is not ToolOutcome.NONE:
        return result.outcome
    if not result.success:
        return ToolOutcome.FAILED
    if not result.output.strip():
        return ToolOutcome.EMPTY
    return ToolOutcome.NONE


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

    execution_timeout: float | None = None
    """P0_3: Per-tool hard execution timeout in seconds.  None = no limit."""

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

    @property
    def prompt_contract(self) -> tuple[str, ...]:
        """Declarative model-facing usage rules supplied with the schema."""
        return ()

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
        """Declare whether this specific call may run beside sibling calls.

        Default derives from ``parallel_safe``:
        - ``parallel_safe=True`` → ``PARALLEL_SAFE``
        - ``parallel_safe=False`` → ``SERIAL``

        Input-aware tools (Bash: ``ls`` vs ``rm``, TaskTool: fork vs
        non-fork) override this method and inspect *params* directly.
        The override always takes precedence over the static property.
        """
        if self.parallel_safe:
            return ToolConcurrency.PARALLEL_SAFE
        return ToolConcurrency.SERIAL

    @property
    def parallel_safe(self) -> bool:
        """Whether this tool can run concurrently with OTHER tools.

        Default: ``False`` (fail-closed — serial execution is always safe).
        Tools that operate on disjoint resources MUST override to ``True``.

        SEPARATE from ``isReadOnly()``.  A tool can be:
        - read-only + parallel-safe   (Read: independent files)
        - read-only + NOT parallel-safe (rate-limited API)
        - write + NOT parallel-safe    (most tools, default)

        This declaration feeds ``concurrency_mode()`` which the
        ``StreamingToolExecutor`` consumes for admission control.
        """
        return False

    def retry_policy(self, params: dict[str, Any]) -> RetryPolicy:
        """Resolve the retry contract for this concrete call.

        Read-only calls retry transient failures automatically. Calls with
        side effects require a fresh interactive approval before every retry.
        Individual tools can override this through metadata.
        """
        configured = getattr(self.metadata, "retry_policy", None)
        if configured is not None:
            return configured
        if self.isReadOnly(params):
            return RetryPolicy(
                mode=RetryMode.AUTOMATIC,
                max_attempts=3,
                idempotency_strategy=IdempotencyStrategy.INVOCATION_KEY,
            )
        return RetryPolicy(
            mode=RetryMode.APPROVAL,
            max_attempts=2,
            idempotency_strategy=IdempotencyStrategy.USER_ACKNOWLEDGED,
        )

    def isReadOnly(self, params: dict[str, Any] | None = None) -> bool:
        """Return True when this call has no side effects (CC-aligned).

        Default: ``False`` (fail-closed — assume writes, requires permission).
        Tools that only read data MUST override this to return ``True``.

        Input-aware tools (e.g. Bash with ``ls`` vs ``rm``) SHOULD inspect
        *params*.  The permission pipeline calls this during plan mode and
        dontAsk mode to decide whether the tool can auto-proceed.

        Fallback: ``ToolMetadata.effects`` — if no override and all declared
        effects fall within ``READ_ONLY_EFFECTS``, the tool is treated as
        read-only.  This catches tools that forgot to override.
        """
        # Check metadata effects as a static fallback
        effects = self.metadata.effects if self.metadata else frozenset()
        if effects:
            from core.policy import READ_ONLY_EFFECTS
            if effects.issubset(READ_ONLY_EFFECTS):
                return True
        return False

    @property
    def supports_cancellation(self) -> bool:
        """Declare whether this tool can respond to mid-execution cancellation.

        Default: ``False`` (fail-closed — most tools cannot be safely
        interrupted).  Only tools that explicitly support cooperative
        cancellation (e.g. Bash with SIGTERM delivery) return ``True``.

        When ``True``, ``ToolExecutionPipeline`` passes a ``CancellationToken``
        into the tool, and the tool is expected to check it periodically or
        register a signal handler.  When ``False``, cancellation requests
        during execution are deferred until the call completes.

        New tool authors MUST explicitly declare this.  The IDE/linter will
        warn on missing overrides (via ``BaseTool`` abstract-like default).
        """
        return False

    @abstractmethod
    def execute(self, params: dict[str, Any]) -> ToolResult:
        """执行工具，返回 ToolResult。不抛异常——所有异常已在内部处理。"""
        ...

    async def aexecute(self, params: dict[str, Any]) -> ToolResult:
        """CC tool.call() 等价 — async 执行工具, 不阻塞事件循环.

        Phase C: async 是工具系统的核心接口。sync 工具默认用 to_thread
        兜底（迁移手段），逐个工具 override 为真 async（Bash → aexec,
        file → async I/O, MCP → bridge.call_tool）。
        """
        import asyncio
        return await asyncio.to_thread(self.execute, params)

    def to_llm_schema(
        self,
        *,
        tier: "ToolDescriptionTier | None" = None,
    ) -> LLMToolSchema:
        """生成供 LLM 使用的 schema，由 ToolRegistry 调用。

        Args:
            tier: Optional description fidelity level.  ``FULL`` (default)
                emits complete description + prompt_contract + parameters.
                ``SUMMARY`` emits a one-line description + parameters
                (no contract).  ``NAME_ONLY`` emits only the tool name.
        """
        from core.types import ToolDescriptionTier
        resolved_tier = tier or ToolDescriptionTier.FULL
        if resolved_tier is ToolDescriptionTier.NAME_ONLY:
            # DEPRECATED: Phase 2 #5 replaced NAME_ONLY with SCHEMA_ONLY.
            # NAME_ONLY is kept for backward compat — same behavior as SCHEMA_ONLY.
            return LLMToolSchema(
                name=self.name,
                description=f"{self.name}: {self.description.split('.')[0]}.",
                parameters=self.parameters_schema,
                tier=ToolDescriptionTier.SCHEMA_ONLY,
            )
        if resolved_tier is ToolDescriptionTier.SCHEMA_ONLY:
            return LLMToolSchema(
                name=self.name,
                description=(
                    self.description.split(".")[0]
                    + "." if "." in self.description else self.description[:80]
                ),
                parameters=self.parameters_schema,
                tier=resolved_tier,
            )
        if resolved_tier is ToolDescriptionTier.SUMMARY:
            return LLMToolSchema(
                name=self.name,
                description=self.description,
                parameters=self.parameters_schema,
                tier=resolved_tier,
            )
        return LLMToolSchema(
            name=self.name,
            description=self.description,
            parameters=self.parameters_schema,
            prompt_contract=self.prompt_contract,
            deferred=(
                bool(getattr(self, "should_defer", False))
                and not bool(getattr(self, "always_load", False))
            ),
            tier=ToolDescriptionTier.FULL,
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

import os as _os
import sys as _sys
from pathlib import Path as _Path

# Platform-adaptive: O_NOFOLLOW is POSIX-only; O_BINARY is Windows-only
_O_NOFOLLOW = getattr(_os, "O_NOFOLLOW", 0)
_O_BINARY = getattr(_os, "O_BINARY", 0)  # Windows: prevent \n→\r\n text conversion


def sanitize_path(user_path: str, workspace_root: str) -> str:
    """Clean user-supplied path: resolve ../, ensure within workspace.

    Layer 1 (Sanitizer): string-level path normalization. Runs BEFORE
    any file operation. Strips ../ traversal attempts without touching
    the filesystem.
    """
    # Phase 1C: null byte injection — reject before any FS call (mkdir/open
    # would otherwise leak ValueError "embedded null character").
    if "\x00" in user_path:
        raise ValueError(
            f"Path '{user_path}' contains a null byte"
        )
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


# ── Protected paths (Phase 1B) ────────────────────────────────────────────
# 对齐 Claude Code：隐式保护关键路径，workspace 内也默认拒绝写入。
DEFAULT_PROTECTED_PATTERNS: tuple[str, ...] = (
    ".git",         # git 元数据目录
    ".env",         # 环境变量/密钥
    "__pycache__",  # Python 缓存
    "node_modules", # npm 依赖
    "*.lock",       # 锁文件
)


def _path_matches_pattern(path_str: str, pattern: str) -> bool:
    """Match *pattern* (glob) against any path segment or the full path."""
    import fnmatch
    norm = path_str.replace("\\", "/")
    pat = pattern.replace("\\", "/")
    # 仅去掉显式的 "./" 前缀；保留 ".env" 这类以点开头的 pattern（lstrip 会误删）
    while pat.startswith("./"):
        pat = pat[2:]
    for part in norm.split("/"):
        if fnmatch.fnmatch(part, pat):
            return True
    return fnmatch.fnmatch(norm, pat) or fnmatch.fnmatch(norm, f"*/{pat}")


def is_path_protected(
    path_str: str,
    extra_patterns: Sequence[str] = (),
    allow_overrides: Sequence[str] = (),
) -> bool:
    """True if *path_str* is a protected path (default or configured).

    Explicit overrides win: if the path matches an *allow_overrides*
    pattern it is NOT protected, regardless of protected patterns.
    """
    protected = tuple(DEFAULT_PROTECTED_PATTERNS) + tuple(extra_patterns)
    for pat in allow_overrides:
        if _path_matches_pattern(path_str, pat):
            return False
    for pat in protected:
        if _path_matches_pattern(path_str, pat):
            return True
    return False


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
    flags = _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC | _O_BINARY
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
    flags = _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL | _O_BINARY
    if _O_NOFOLLOW:
        flags |= _O_NOFOLLOW
    try:
        fd = _os.open(full_path, flags)
        return fd, ""
    except OSError as e:
        return None, f"Cannot create '{full_path}': {e}"


def atomic_write_bytes(full_path: str, data: bytes) -> tuple[str, str]:
    """Atomically write *data* to *full_path* via tmp-file + os.replace.

    Replaces the previous O_TRUNC in-place write with a tmp+rename atomic
    pattern, so a crash mid-write never leaves a truncated target file.
    Preserves the TOCTOU/symlink protections of ``safe_open_for_write``:
    - POSIX: opens the tmp file with O_NOFOLLOW (kernel rejects symlinks);
    - Windows: explicit is_symlink() check (no kernel symlink TOCTOU).

    Returns ``(full_path, error)``; ``error`` is "" on success.
    """
    p = _Path(full_path)
    if _sys.platform == "win32" and p.exists() and p.is_symlink():
        return "", f"Cannot write to symlink: {full_path}"
    tmp = p.with_name(f".{p.name}.tmp.{_os.getpid()}")
    flags = _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC | _O_BINARY
    if _O_NOFOLLOW:
        flags |= _O_NOFOLLOW
    try:
        fd = _os.open(str(tmp), flags)
        try:
            _os.write(fd, data)
        finally:
            _os.close(fd)
    except OSError as e:
        return "", f"Cannot write '{full_path}': {e}"
    try:
        _os.replace(str(tmp), full_path)
    except OSError as e:
        try:
            _os.unlink(str(tmp))
        except OSError:
            pass
        return "", f"Cannot replace '{full_path}': {e}"
    return full_path, ""


# ── Lightweight syntax validation + immediate rollback (Phase 1A) ───────────
# 对齐 Claude Code "不污染工作区"原则：写入要么成功且语法有效，要么回滚。
# 不做全量 AST（避免语言依赖）——按扩展名路由到轻量校验器。

_EXT_SYNTAX_KIND = {
    ".py": "py",
    ".json": "json",
    ".js": "js",
    ".mjs": "js",
    ".cjs": "js",
}


def syntax_kind_for_path(path_str: str) -> str:
    """Return the syntax-checker kind for *path_str* (e.g. "py", "json"), "" if unsupported."""
    return _EXT_SYNTAX_KIND.get(_Path(path_str).suffix.lower(), "")


def _syntax_check(kind: str, content: str) -> str:
    """Run a lightweight syntax checker. Returns '' if valid, else error text.

    - py    → py_compile (no runtime execution, syntax only)
    - json  → json.loads
    - js    → node --check (subprocess, 5s timeout; skipped if node missing)
    """
    if kind == "py":
        import py_compile
        import tempfile
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".py", delete=False, encoding="utf-8",
            ) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            try:
                py_compile.compile(tmp_path, doraise=True)
                return ""
            finally:
                try:
                    _os.unlink(tmp_path)
                except OSError:
                    pass
        except py_compile.PyCompileError as e:
            return f"Python syntax error: {e}"
        except Exception as e:
            return f"Syntax check failed ({type(e).__name__}): {e}"
    if kind == "json":
        import json
        try:
            json.loads(content)
            return ""
        except Exception as e:
            return f"JSON syntax error: {e}"
    if kind == "js":
        import subprocess
        import tempfile
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".js", delete=False, encoding="utf-8",
            ) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            try:
                proc = subprocess.run(
                    ["node", "--check", tmp_path],
                    capture_output=True, text=True, timeout=5,
                )
                if proc.returncode == 0:
                    return ""
                return f"JS syntax error: {proc.stderr.strip() or proc.stdout.strip()}"
            finally:
                try:
                    _os.unlink(tmp_path)
                except OSError:
                    pass
        except FileNotFoundError:
            return ""  # node unavailable → skip (do not block writes)
        except Exception as e:
            return f"Syntax check failed ({type(e).__name__}): {e}"
    return ""


def validate_content_for_path(path_str: str, content: str) -> str:
    """Lightweight syntax validation for content destined for *path_str*.

    Returns '' if valid or unsupported extension, else a human-readable
    error message.  Used by atomic_write_checked and fallback write paths.
    """
    kind = syntax_kind_for_path(path_str)
    if not kind:
        return ""
    return _syntax_check(kind, content)


def _restore_file(full_path: str, original: bytes | None) -> None:
    """Atomically restore *original* bytes to *full_path*, or delete it if created new."""
    if original is None:
        try:
            _os.unlink(full_path)
        except OSError:
            pass
        return
    tmp = f"{full_path}.restore.{_os.getpid()}"
    try:
        with open(tmp, "wb") as f:
            f.write(original)
        _os.replace(tmp, full_path)
    finally:
        try:
            if _os.path.exists(tmp):
                _os.unlink(tmp)
        except OSError:
            pass


def atomic_write_checked(full_path: str, data: bytes,
                         path_for_kind: str = "") -> tuple[str, str]:
    """Atomic write + lightweight syntax validation + immediate rollback.

    对齐 CC "不污染工作区"：写入要么成功且语法有效，要么回滚并返回错误。
    校验失败时自动恢复原文件内容（原子写回），不留 .tmp 残留。

    Returns (path, error); error is "" on success.
    """
    # 1. Backup existing content (byte-exact) for rollback
    original: bytes | None = None
    if _os.path.exists(full_path):
        try:
            with open(full_path, "rb") as f:
                original = f.read()
        except OSError:
            original = None

    # 2. Atomic write (tmp + os.replace)
    written, err = atomic_write_bytes(full_path, data)
    if err:
        return "", err

    # 3. Lightweight syntax validation
    verr = validate_content_for_path(path_for_kind, data.decode("utf-8", errors="replace"))
    if verr:
        # 4. Rollback — restore original bytes atomically
        _restore_file(full_path, original)
        return "", verr

    return written, ""


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
        permission_pipeline: Any = None,
        hook_dispatcher: Any = None,
        tool_availability_guard: Any = None,
        *,
        artifact_store_ref: Any = None,
        skill_registry: Any = None,
        skill_buffer: Any = None,
        mcp_integration: Any = None,
    ) -> None:
        """Create a tool registry with optional Runtime-owned intercept layers.

        All parameters use ``Any`` at runtime to avoid circular imports from
        hitl/hooks packages (P2-10).
        """
        self._tools: dict[str, BaseTool] = {}
        self._tool_aliases: dict[str, str] = {}
        self._permission_pipeline = permission_pipeline
        self._hook_dispatcher = hook_dispatcher
        self._tool_availability_guard = tool_availability_guard
        self._artifact_store_ref = artifact_store_ref
        self._skill_registry = skill_registry
        self._skill_buffer = skill_buffer
        self._mcp_integration = mcp_integration
        self._closeables: list[Any] = []
        self._closed = False
        self._owns_lifecycle = True
        self._pending_skill_modifier: Any = None
        self._resource_governor = None
        self._root_session_resolver = None
        self._structure_lock = threading.RLock()
        self._stats_lock = threading.Lock()
        """Protects ``_timing_stats`` — multiple threads call ``execute_tool``
        concurrently in Web mode (ACC-4a)."""
        self._timing_stats: dict[str, dict[str, float | int]] = {}

    def register(self, tool: BaseTool) -> "ToolRegistry":
        """
        Register a tool. Supports chaining:
            registry.register(ShellTool()).register(FileTool())

        Phase 3: namespace collision resolution — priority-based rejection.
        system(3) > project(2) > mcp(1). Same priority → first-wins.
        Per-session: collision only matters within one build_registry_for_session() call.
        """
        from tools.factory import build_tool

        tool = build_tool(tool=tool)
        with self._structure_lock:
            existing = self._tools.get(tool.name)
            if existing is not None:
                self._resolve_collision(tool.name, existing, tool)
            if tool.name in self._tools and self._tools[tool.name] is not tool:
                # Collision resolution rejected the new tool — skip registration
                return self
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

    def _resolve_collision(
        self, name: str, existing: BaseTool, new_tool: BaseTool,
    ) -> None:
        """Phase 3: resolve name conflict via source priority.

        Higher priority replaces lower. Same priority → first-wins (reject new).
        Always logs WARNING so collisions are never silent.
        """
        existing_source = self._tool_source_for(existing)
        new_source = self._tool_source_for(new_tool)
        existing_pri = TOOL_SOURCE_PRIORITY.get(existing_source, 0)
        new_pri = TOOL_SOURCE_PRIORITY.get(new_source, 0)

        if new_pri > existing_pri:
            logger.warning(
                "Tool '%s' collision: %r (source=%s, priority=%d) replaces "
                "%r (source=%s, priority=%d)",
                name, type(new_tool).__name__, new_source, new_pri,
                type(existing).__name__, existing_source, existing_pri,
            )
            del self._tools[name]  # allow replacement
        elif new_pri == existing_pri:
            logger.warning(
                "Tool '%s' collision: %r (source=%s) rejected — same priority "
                "as existing %r (source=%s). First-wins semantics.",
                name, type(new_tool).__name__, new_source,
                type(existing).__name__, existing_source,
            )
        else:
            logger.warning(
                "Tool '%s' collision: %r (source=%s, priority=%d) rejected — "
                "lower priority than existing %r (source=%s, priority=%d)",
                name, type(new_tool).__name__, new_source, new_pri,
                type(existing).__name__, existing_source, existing_pri,
            )

    @staticmethod
    def _tool_source_for(tool: BaseTool) -> str:
        """Extract canonical source from tool metadata (Phase 3)."""
        return getattr(getattr(tool, "metadata", None), "source", "") or "system"

    def register_many(self, tools: Iterable[BaseTool]) -> "ToolRegistry":
        for tool in tools:
            self.register(tool)
        return self

    def register_plugin(self, config: "Any") -> list[str]:
        """Register tools from a ``ToolPlugin`` configuration entry.

        Resolves the plugin by name from the in-process registry,
        validates config, creates tools, and registers them normally.
        Returns the list of registered tool names.

        Raises ``ValueError`` if the plugin cannot be resolved or
        config validation fails.
        """
        from core.tool_plugin import resolve_plugin
        plugin = resolve_plugin(config.plugin)
        if plugin is None:
            raise ValueError(
                f"Plugin {config.plugin!r} not found in registry. "
                f"Registered plugins: {list(_plugin_registry.keys())}"
            )
        plugin.validate_config(config.config)
        tool = plugin.create_tool(config.config)
        self.register(tool)
        return [tool.name]

    def unregister(self, name: str) -> BaseTool | None:
        """Remove one canonical tool and all aliases that point to it."""
        canonical = self.resolve_name(name)
        if canonical is None:
            return None
        with self._structure_lock:
            tool = self._tools.pop(canonical)
            self._tool_aliases = {
                alias: target
                for alias, target in self._tool_aliases.items()
                if target != canonical
            }
        return tool

    @property
    def artifact_store_ref(self) -> Any:
        return self._artifact_store_ref

    @property
    def skill_registry(self) -> Any:
        return self._skill_registry

    @property
    def skill_buffer(self) -> Any:
        return self._skill_buffer

    @property
    def mcp_integration(self) -> Any:
        return self._mcp_integration

    def attach_mcp_integration(self, integration: Any) -> None:
        """Attach the canonical MCP activation owner after bootstrap."""
        self._mcp_integration = integration

    def activate_mcp_servers(
        self,
        server_names: set[str] | frozenset[str],
    ) -> list[str]:
        """Activate deferred tools owned by declared MCP server dependencies."""
        if self._mcp_integration is None or not server_names:
            return []
        return self._mcp_integration.activate_servers(set(server_names))

    @property
    def tool_availability_guard(self) -> Any:
        return self._tool_availability_guard

    def add_closeable(self, value: Any) -> None:
        if value is not None and value not in self._closeables:
            self._closeables.append(value)

    def activate_skill(self, metadata: Any) -> None:
        """Queue one turn-scoped Skill modifier for the next policy view."""
        from skills.tool import SkillContextModifier

        mcp_servers = frozenset(getattr(metadata, "mcp_servers", frozenset()))
        self._pending_skill_modifier = SkillContextModifier(
            allowed_tools=metadata.allowed_tools,
            disallowed_tools=metadata.disallowed_tools,
            mcp_servers=mcp_servers,
            model=metadata.model,
            effort=metadata.effort,
            context=metadata.context,
        )
        self.activate_mcp_servers(mcp_servers)

    def consume_skill_modifier(self) -> Any:
        modifier = self._pending_skill_modifier
        self._pending_skill_modifier = None
        return modifier

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

    def _get_evidence_recorder(self, store: Any) -> Any:
        """Get or create the cached ToolEvidenceRecorder for *store*."""
        cached = getattr(self, "_evidence_recorder", None)
        if cached is not None and cached._store is store:
            return cached
        from agent.session.tool_evidence_recorder import ToolEvidenceRecorder
        _scope = getattr(self, "_evidence_scope", None)
        recorder = ToolEvidenceRecorder(store, scope=_scope)
        self._evidence_recorder = recorder
        return recorder

    def execute_tool(
        self,
        name: str,
        params: dict[str, Any],
        thought: str = "",
        *,
        invocation_id: str = "",
        cancel_token: object | None = None,
    ) -> ToolResult:
        """
        按名称查找工具并执行。
        所有调用统一经过 PermissionPipeline 审批。
        工具不存在时返回 error ToolResult（不抛异常，让 agent 继续运行）。

        P0_3 Batch 2: cancel_token forwarded to ToolExecutionPipeline
        → ResourceGovernor for queue-wait cancellation.
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
        # T27: DEPRECATED — Native path uses composition/_execute_via_registry
        # which delegates to tool.execute() directly with retry + validation.
        from core.tool_execution import ToolExecutionPipeline

        # ── Evidence recorder (cached per-store) ──
        _evidence_recorder = None
        _evidence_store = getattr(self, "_evidence_store", None)
        if _evidence_store is not None:
            _evidence_recorder = self._get_evidence_recorder(_evidence_store)

        pipeline = ToolExecutionPipeline(
            permission_pipeline=self._permission_pipeline,
            hook_dispatcher=self._hook_dispatcher,
            capability_registry=self._tool_availability_guard,
            session_id=getattr(self, "_session_id", ""),
            budget=getattr(self, "_budget", None),
            resource_governor=getattr(self, "_resource_governor", None),
            root_session_resolver=getattr(
                self, "_root_session_resolver", None
            ),
            evidence_recorder=_evidence_recorder,
        )
        result = pipeline.execute(
            tool,
            params,
            thought=thought,
            invocation_id=invocation_id,
            cancel_token=cancel_token,
        )

        self._record_timing(name, start, result)
        return result

    def get_schemas(self) -> list[LLMToolSchema]:
        """Return schemas with a stable built-in prefix and MCP suffix."""
        from tools.pool import assemble_tool_pool, is_mcp_tool
        with self._structure_lock:
            snapshot = tuple(self._tools.values())
        tools = assemble_tool_pool(
            (tool for tool in snapshot if not is_mcp_tool(tool)),
            (tool for tool in snapshot if is_mcp_tool(tool)),
        )
        schemas = [
            schema
            for tool in tools
            if not (schema := tool.to_llm_schema()).deferred
        ]
        return schemas

    @property
    def tool_names(self) -> list[str]:
        with self._structure_lock:
            return list(self._tools.keys())

    @property
    def tools(self) -> tuple[BaseTool, ...]:
        with self._structure_lock:
            return tuple(self._tools.values())

    def filtered(self, allowed_tools: set[str] | frozenset[str]) -> "ToolRegistry":
        """返回只包含指定工具的新注册表，保留所有拦截层（pipeline, HITL, hooks, capability）。"""
        filtered = ToolRegistry(
            permission_pipeline=self._permission_pipeline,
            hook_dispatcher=self._hook_dispatcher,
            tool_availability_guard=self._tool_availability_guard,
            artifact_store_ref=self._artifact_store_ref,
            skill_registry=self._skill_registry,
            skill_buffer=self._skill_buffer,
            mcp_integration=self._mcp_integration,
        )
        for tool_name in self.tool_names:
            if tool_name in allowed_tools:
                filtered._tools[tool_name] = self._tools[tool_name]
        filtered._resource_governor = self._resource_governor
        filtered._root_session_resolver = self._root_session_resolver
        filtered._closeables = self._closeables
        filtered._owns_lifecycle = False
        filtered._pending_skill_modifier = self._pending_skill_modifier
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
        derived._owns_lifecycle = False
        derived._timing_stats = {}
        pipeline = self._permission_pipeline
        if isinstance(pipeline, AgentScopablePermissionPipeline):
            derived._permission_pipeline = pipeline.for_agent(agent_name)
        return derived

    def configure_permission_session(self, config: Any) -> None:
        """Configure session authorization without exposing pipeline internals."""
        pipeline = self._permission_pipeline
        configure = getattr(pipeline, "configure_session", None)
        if callable(configure):
            configure(config)

    def permission_control_signal(self) -> Any:
        """Return the permission layer's immutable Runtime control signal."""
        signal = getattr(self._permission_pipeline, "control_signal", None)
        return signal() if callable(signal) else None

    def attach_hook_dispatcher(self, dispatcher: Any) -> None:
        """Attach hooks to both permission and post-execution boundaries."""
        self._hook_dispatcher = dispatcher
        attach = getattr(self._permission_pipeline, "attach_hook_dispatcher", None)
        if callable(attach):
            attach(dispatcher)

    def attach_resource_governor(
        self,
        governor: Any,
        *,
        root_session_resolver: Any = None,
    ) -> None:
        """Attach the single resource authority used by external tools."""
        self._resource_governor = governor
        self._root_session_resolver = root_session_resolver

    @property
    def hook_dispatcher(self) -> Any:
        """Read-only access to the configured lifecycle dispatcher."""
        return self._hook_dispatcher

    def with_session_id(self, session_id: str) -> "ToolRegistry":
        """Return a shallow session-tagged registry view."""
        derived = copy.copy(self)
        derived._owns_lifecycle = False
        derived._session_id = session_id
        return derived

    def permission_inheritable_state(self) -> dict:
        """Export child-safe permission state through the registry boundary."""
        getter = getattr(self._permission_pipeline, "get_inheritable_state", None)
        return getter() if callable(getter) else {}

    def apply_inherited_permission_state(
        self, state: dict, *, child_permission_mode: str,
    ) -> None:
        """Apply inherited permission state without exposing the evaluator."""
        apply_state = getattr(
            self._permission_pipeline, "apply_inherited_state", None,
        )
        if callable(apply_state):
            apply_state(
                state,
                child_permission_mode=child_permission_mode,
            )

    def scoped(self, context: ExecutionContext) -> "ToolRegistry":
        """Clone registered tools into an isolated per-session context."""
        permission_pipeline = self._permission_pipeline
        if isinstance(permission_pipeline, ProjectScopablePermissionPipeline):
            permission_pipeline = permission_pipeline.scoped(
                context.repo_path or context.workspace_root
            )
        scoped = ToolRegistry(
            permission_pipeline=permission_pipeline,
            hook_dispatcher=self._hook_dispatcher,
            tool_availability_guard=self._tool_availability_guard,
            artifact_store_ref=self._artifact_store_ref,
            skill_registry=self._skill_registry,
            skill_buffer=self._skill_buffer,
            mcp_integration=self._mcp_integration,
        )
        scoped._resource_governor = self._resource_governor
        scoped._root_session_resolver = self._root_session_resolver
        scoped._owns_lifecycle = False
        for tool in self._tools.values():
            scoped.register(tool.bind_context(context))
        return scoped

    def with_run_context(self, context: Any) -> "ToolRegistry":
        """Clone only tools that declaratively consume per-run resources."""
        # Preserve registry-level dependency references and session metadata;
        # only tool instances and per-run counters belong to the new binding.
        bound = copy.copy(self)
        bound._owns_lifecycle = False
        bound._tools = {}
        bound._tool_aliases = {}
        bound._timing_stats = {}
        # Carry evidence_store and scope from RunContext
        bound._evidence_store = getattr(context, "evidence_store", None)
        bound._evidence_scope = getattr(context, "evidence_scope", None)
        bound._evidence_recorder = None  # lazily created by _get_evidence_recorder()
        for tool in self._tools.values():
            bound.register(
                tool.with_run_context(context)
                if isinstance(tool, RunContextAware)
                else tool
            )
        return bound

    def close(self, timeout: float = 5.0) -> None:
        """Close registered lifecycle owners exactly once."""
        if not self._owns_lifecycle:
            return
        if self._closed:
            return
        self._closed = True
        seen: set[int] = set()
        values = [*self._tools.values(), *self._closeables]
        if self._skill_registry is not None:
            values.append(self._skill_registry)
        for value in reversed(values):
            if id(value) in seen:
                continue
            seen.add(id(value))
            close = getattr(value, "close", None)
            if not callable(close):
                close = getattr(value, "shutdown", None)
            if not callable(close):
                continue
            try:
                close(timeout=timeout)
            except TypeError:
                close()
            except Exception:
                logger.warning(
                    "Tool lifecycle close failed for %r",
                    value,
                    exc_info=True,
                )
        if self._skill_buffer is not None:
            clear = getattr(self._skill_buffer, "clear", None)
            if callable(clear):
                clear()

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __repr__(self) -> str:
        return f"ToolRegistry(tools={self.tool_names})"

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
