"""
Streaming tool executor with sibling-abort and discard semantics.

This is the canonical implementation. ``runtime/sibling_abort.py`` re-exports
from here for backward compatibility.

Design notes aligned with Claude Code StreamingToolExecutor.ts:
- Conservative mode: add tools, then execute_all() partitions into batches.
- Aggressive mode: add_tool(..., execute_fn=...) starts work immediately.
- Concurrency-safe tools can overlap with other safe tools.
- Non-concurrency-safe tools wait for exclusive access.
- When a tool fails, siblingAbort aborts remaining tools in the active batch.
- discard() cancels every QUEUED / EXECUTING tool and marks it CANCELLED.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from runtime.tool import ToolCall, ToolExecutionResult, ToolUseContext

if TYPE_CHECKING:
    from runtime.tool_registry import ToolRegistry


class ToolStatus(Enum):
    """Lifecycle states for a tracked tool execution."""

    QUEUED = "queued"
    EXECUTING = "executing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERRORED = "errored"


@dataclass
class TrackedTool:
    """Wraps a single tool call with execution metadata."""

    tool_call: Any
    status: ToolStatus = ToolStatus.QUEUED
    is_concurrency_safe: bool = False
    result: ToolExecutionResult | None = None
    error: Exception | None = None
    _task: asyncio.Task | None = field(default=None, repr=False)
    _execute_fn: "ExecuteFn | None" = field(default=None, repr=False)


ExecuteFn = Callable[[Any], Awaitable[Any]]
ContextModifier = Callable[[Any], Any]
MAX_CONCURRENCY = int(os.environ.get("CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY", "10") or "10")


class SiblingAbortController:
    """Scoped cancellation signal for sibling tools within one batch."""

    def __init__(self) -> None:
        self._aborted = False
        self._reason: str | None = None

    @property
    def is_aborted(self) -> bool:
        return self._aborted

    @property
    def reason(self) -> str | None:
        return self._reason

    def abort(self, reason: str = "sibling tool failed") -> None:
        self._aborted = True
        self._reason = reason


class StreamingToolExecutor:
    """Batch-aware streaming tool executor with sibling-abort and discard."""

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        context: ToolUseContext | None = None,
    ) -> None:
        self.tools: list[TrackedTool] = []
        self.has_errored: bool = False
        self.sibling_abort = SiblingAbortController()
        self._discarded: bool = False
        self._registry = registry
        self._context = context or ToolUseContext()
        self._history: list[ToolExecutionResult] = []
        self._queue: list[TrackedTool] = []
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
        self._context_modifiers: dict[str, list[ContextModifier]] = {}

    def can_execute_tool(self, is_concurrency_safe: bool) -> bool:
        """Return whether a new tool can run with currently executing tools."""
        executing = [tool for tool in self.tools if tool.status == ToolStatus.EXECUTING]
        return (
            len(executing) == 0
            or (
                is_concurrency_safe
                and all(tool.is_concurrency_safe for tool in executing)
            )
        )

    def add_tool(
        self,
        tool_call: Any,
        *,
        is_concurrency_safe: bool | Callable[[dict], bool] | None = None,
        execute_fn: ExecuteFn | None = None,
    ) -> None:
        """
        Register a tool call.

        If execute_fn is provided, the executor enters aggressive mode: the tool
        is started immediately when concurrency rules allow it, otherwise it is
        queued and started by _process_queue() when prior work completes.
        """
        if self._discarded:
            return

        if callable(is_concurrency_safe):
            is_concurrency_safe = self._resolve_concurrency_safe(tool_call, is_concurrency_safe)
        elif is_concurrency_safe is None:
            is_concurrency_safe = self._is_tool_concurrency_safe(tool_call)
        else:
            is_concurrency_safe = bool(is_concurrency_safe)

        tracked = TrackedTool(
            tool_call=tool_call,
            is_concurrency_safe=is_concurrency_safe,
            _execute_fn=execute_fn,
        )
        self.tools.append(tracked)

        if execute_fn is None:
            return

        self._queue.append(tracked)
        self._process_queue()

    def discard(self) -> None:
        """Cancel every QUEUED / EXECUTING tool and mark it CANCELLED."""
        self._discarded = True
        self._queue.clear()

        for tracked in self.tools:
            if tracked.status == ToolStatus.QUEUED:
                tracked.status = ToolStatus.CANCELLED
                tracked.result = self._make_cancelled_result(tracked.tool_call)
            elif tracked.status == ToolStatus.EXECUTING:
                tracked.status = ToolStatus.CANCELLED
                tracked.result = self._make_cancelled_result(tracked.tool_call)
                if tracked._task is not None and not tracked._task.done():
                    tracked._task.cancel()

    @property
    def is_discarded(self) -> bool:
        return self._discarded

    async def execute_all(self, execute_fn: ExecuteFn | None = None) -> list[ToolExecutionResult]:
        """Execute all queued tools respecting batch partitioning."""
        if execute_fn is None:
            execute_fn = self._execute_from_registry

        if self._discarded:
            return [tool.result for tool in self.tools if tool.result is not None]

        pending = [
            tool for tool in self.tools
            if tool.status == ToolStatus.QUEUED and tool._task is None
        ]
        batches = self._partition_batches(pending)

        for batch in batches:
            if self.sibling_abort.is_aborted:
                for tracked in batch:
                    if tracked.status == ToolStatus.QUEUED:
                        tracked.status = ToolStatus.CANCELLED
                        tracked.result = self._make_cancelled_result(tracked.tool_call)
                continue

            if batch[0].is_concurrency_safe:
                await self._run_batch_concurrent(batch, execute_fn)
            else:
                await self._run_batch_serial(batch, execute_fn)

        results = [tool.result for tool in self.tools if tool.result is not None]
        self._history.extend(result for result in results if result not in self._history)
        return results

    async def get_remaining_results(self) -> list[ToolExecutionResult]:
        """
        Collect all results for aggressive mode.

        Completed results are returned immediately. Executing tasks are awaited.
        Queued tasks are allowed to drain through the queue processor. Results
        are returned in original tool insertion order.
        """
        while True:
            self._process_queue()
            running = [
                tool._task for tool in self.tools
                if tool.status == ToolStatus.EXECUTING
                and tool._task is not None
                and not tool._task.done()
            ]
            queued = [tool for tool in self.tools if tool.status == ToolStatus.QUEUED]

            if not running and not queued:
                break

            if running:
                await asyncio.gather(*running, return_exceptions=True)
            else:
                for tracked in queued:
                    tracked.status = ToolStatus.CANCELLED
                    tracked.result = self._make_cancelled_result(tracked.tool_call)
                break

        results: list[ToolExecutionResult] = []
        for tracked in self.tools:
            if tracked.result is None:
                if tracked.status == ToolStatus.CANCELLED:
                    tracked.result = self._make_cancelled_result(tracked.tool_call)
                elif tracked.status == ToolStatus.ERRORED and tracked.error is not None:
                    tracked.result = self._make_error_result(tracked.tool_call, tracked.error)
                elif tracked.status == ToolStatus.QUEUED:
                    tracked.status = ToolStatus.CANCELLED
                    tracked.result = self._make_cancelled_result(tracked.tool_call)

            if tracked.result is not None:
                results.append(tracked.result)

        self._history.extend(result for result in results if result not in self._history)
        return results

    @property
    def has_pending(self) -> bool:
        return any(tool.status == ToolStatus.QUEUED for tool in self.tools)

    @property
    def all_results(self) -> list[ToolExecutionResult]:
        return list(self._history)

    def apply_context_modifiers(self, context: Any) -> Any:
        """Apply queued context modifiers in original tool-call order."""
        current = context
        for tracked in self.tools:
            for modifier in self._context_modifiers.get(_tool_call_id(tracked.tool_call), []):
                current = modifier(current)
        return current

    async def _run_batch_concurrent(self, batch: list[TrackedTool], execute_fn: ExecuteFn) -> None:
        """Run a concurrency-safe batch in parallel."""
        tasks: list[asyncio.Task] = []

        for tracked in batch:
            if self.sibling_abort.is_aborted:
                tracked.status = ToolStatus.CANCELLED
                tracked.result = self._make_cancelled_result(tracked.tool_call)
                continue

            self._start_execution(tracked, execute_fn, process_queue_on_done=False)
            if tracked._task is not None:
                tasks.append(tracked._task)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_batch_serial(self, batch: list[TrackedTool], execute_fn: ExecuteFn) -> None:
        """Run a non-concurrency-safe batch one-by-one."""
        for tracked in batch:
            if self.sibling_abort.is_aborted:
                tracked.status = ToolStatus.CANCELLED
                tracked.result = self._make_cancelled_result(tracked.tool_call)
                continue

            tracked.status = ToolStatus.EXECUTING
            await self._run_single(tracked, execute_fn)

    def _start_execution(
        self,
        tracked: TrackedTool,
        execute_fn: ExecuteFn,
        *,
        process_queue_on_done: bool = True,
    ) -> None:
        """Start one tool execution in an asyncio task."""
        if tracked.status != ToolStatus.QUEUED or self._discarded:
            return

        tracked.status = ToolStatus.EXECUTING
        tracked._execute_fn = execute_fn
        task = asyncio.create_task(self._run_single(tracked, execute_fn))
        tracked._task = task
        if process_queue_on_done:
            task.add_done_callback(lambda _task: self._process_queue())

    def _process_queue(self) -> None:
        """Start queued aggressive-mode tools when concurrency rules allow."""
        if self._discarded or self.sibling_abort.is_aborted:
            for tracked in list(self._queue):
                if tracked.status == ToolStatus.QUEUED:
                    tracked.status = ToolStatus.CANCELLED
                    tracked.result = self._make_cancelled_result(tracked.tool_call)
            self._queue.clear()
            return

        remaining: list[TrackedTool] = []
        index = 0
        while index < len(self._queue):
            tracked = self._queue[index]
            if tracked.status != ToolStatus.QUEUED:
                index += 1
                continue

            execute_fn = tracked._execute_fn
            if execute_fn is None:
                remaining.extend(self._queue[index:])
                break

            if tracked.is_concurrency_safe:
                if self._has_non_safe_running():
                    remaining.extend(self._queue[index:])
                    break
                self._start_execution(tracked, execute_fn)
                index += 1
                continue

            if self._has_running_tools():
                remaining.extend(self._queue[index:])
                break
            self._start_execution(tracked, execute_fn)
            index += 1
            remaining.extend(self._queue[index:])
            break

        self._queue = [tool for tool in remaining if tool.status == ToolStatus.QUEUED]

    def _has_running_tools(self) -> bool:
        return any(tool.status == ToolStatus.EXECUTING for tool in self.tools)

    def _has_non_safe_running(self) -> bool:
        return any(
            tool.status == ToolStatus.EXECUTING and not tool.is_concurrency_safe
            for tool in self.tools
        )

    async def _run_single(self, tracked: TrackedTool, execute_fn: ExecuteFn) -> None:
        """Execute one tool and handle success / failure / cancellation."""
        if self.sibling_abort.is_aborted:
            tracked.status = ToolStatus.CANCELLED
            tracked.result = self._make_cancelled_result(tracked.tool_call)
            return

        try:
            if tracked.is_concurrency_safe:
                async with self._semaphore:
                    result = await execute_fn(tracked.tool_call)
            else:
                result = await execute_fn(tracked.tool_call)

            modifiers = _extract_context_modifiers(result)
            if modifiers:
                self._context_modifiers[_tool_call_id(tracked.tool_call)] = modifiers

            if _result_is_error(result):
                tracked.status = ToolStatus.ERRORED
                tracked.result = _coerce_execution_result(tracked.tool_call, result)
                self.has_errored = True
                self.sibling_abort.abort(
                    reason=f"tool '{_tool_name(tracked.tool_call)}' failed: {_result_error(result)}"
                )
                await self._cancel_siblings(tracked)
                return
            tracked.status = ToolStatus.COMPLETED
            tracked.result = _coerce_execution_result(tracked.tool_call, result)

        except asyncio.CancelledError:
            tracked.status = ToolStatus.CANCELLED
            tracked.result = self._make_cancelled_result(tracked.tool_call)

        except Exception as exc:
            tracked.status = ToolStatus.ERRORED
            tracked.error = exc
            tracked.result = self._make_error_result(tracked.tool_call, exc)
            self.has_errored = True
            self.sibling_abort.abort(
                reason=f"tool '{_tool_name(tracked.tool_call)}' failed: {exc}"
            )
            await self._cancel_siblings(tracked)

    async def _cancel_siblings(self, failed: TrackedTool) -> None:
        """Cancel all EXECUTING siblings of the failed tool."""
        siblings: list[asyncio.Task] = []
        for tracked in self.tools:
            if (
                tracked is not failed
                and tracked.status == ToolStatus.EXECUTING
                and tracked._task is not None
                and not tracked._task.done()
            ):
                tracked.status = ToolStatus.CANCELLED
                tracked.result = self._make_cancelled_result(tracked.tool_call)
                tracked._task.cancel()
                siblings.append(tracked._task)
            elif tracked is not failed and tracked.status == ToolStatus.QUEUED:
                tracked.status = ToolStatus.CANCELLED
                tracked.result = self._make_cancelled_result(tracked.tool_call)

        self._queue = [tool for tool in self._queue if tool.status == ToolStatus.QUEUED]

        if siblings:
            await asyncio.gather(*siblings, return_exceptions=True)

    async def _execute_from_registry(self, tool_call: ToolCall) -> ToolExecutionResult:
        """Execute a tool through the configured registry, if available."""
        if self._registry is None:
            return ToolExecutionResult(
                call_id=_tool_call_id(tool_call),
                tool_name=_tool_name(tool_call),
                error="No tool registry configured",
            )

        tool = self._registry.find_by_name(_tool_name(tool_call))
        if tool is None:
            return ToolExecutionResult(
                call_id=_tool_call_id(tool_call),
                tool_name=_tool_name(tool_call),
                error=f"Unknown tool: {_tool_name(tool_call)}",
            )

        from runtime.tool_executor import execute_single_tool

        return await execute_single_tool(tool, tool_call, self._context)

    def _resolve_concurrency_safe(self, tool_call: Any, fn: Callable[[dict], bool]) -> bool:
        """Resolve an explicit concurrency predicate, fail-closed."""
        try:
            tool_input = tool_call.input if hasattr(tool_call, "input") else tool_call.get("input", {})
            if not isinstance(tool_input, dict):
                return False
            return bool(fn(tool_input))
        except Exception:
            return False

    def _is_tool_concurrency_safe(self, tool_call: Any) -> bool:
        """Return concurrency safety from the configured registry, fail-closed."""
        if self._registry is None:
            return False

        try:
            tool = self._registry.find_by_name(_tool_name(tool_call))
        except Exception:
            return False

        if tool is None:
            return False

        try:
            tool_input = tool_call.input if hasattr(tool_call, "input") else tool_call.get("input", {})
            # Current ToolCall.input is object-shaped. This is a simplified
            # schema-parse success gate; if scalar tool inputs are added later,
            # replace this with per-tool schema validation instead of allowing
            # unknown shapes through.
            if not isinstance(tool_input, dict):
                return False
            return bool(tool.is_concurrency_safe(tool_input))
        except Exception:
            return False

    @staticmethod
    def _partition_batches(tools: list[TrackedTool]) -> list[list[TrackedTool]]:
        """Split tools into contiguous batches of the same concurrency-safety flag."""
        if not tools:
            return []

        batches: list[list[TrackedTool]] = []
        current: list[TrackedTool] = []
        current_safe: bool | None = None

        for tracked in tools:
            if current_safe is not None and tracked.is_concurrency_safe != current_safe:
                batches.append(current)
                current = []

            current.append(tracked)
            current_safe = tracked.is_concurrency_safe

        if current:
            batches.append(current)

        return batches

    @staticmethod
    def _make_cancelled_result(tool_call: Any) -> ToolExecutionResult:
        """Synthetic result for a tool cancelled before completion."""
        return ToolExecutionResult(
            call_id=_tool_call_id(tool_call),
            tool_name=_tool_name(tool_call),
            error="[cancelled] Tool execution was cancelled due to a sibling tool failure.",
        )

    @staticmethod
    def _make_error_result(tool_call: Any, exc: Exception) -> ToolExecutionResult:
        """Synthetic result for a tool that raised an exception."""
        return ToolExecutionResult(
            call_id=_tool_call_id(tool_call),
            tool_name=_tool_name(tool_call),
            error=f"Error: {exc}",
        )


def _coerce_execution_result(tool_call: Any, result: Any) -> ToolExecutionResult:
    if isinstance(result, ToolExecutionResult):
        return result
    if isinstance(result, dict):
        return ToolExecutionResult(
            call_id=str(result.get("call_id", result.get("tool_call_id", result.get("id", _tool_call_id(tool_call))))),
            tool_name=str(result.get("tool_name", result.get("name", _tool_name(tool_call)))),
            error=str(result["error"]) if result.get("error") is not None else None,
        )
    return ToolExecutionResult(
        call_id=_tool_call_id(tool_call),
        tool_name=_tool_name(tool_call),
        result=result,
    )


def _result_is_error(result: Any) -> bool:
    if isinstance(result, ToolExecutionResult):
        return result.error is not None
    if isinstance(result, dict):
        return bool(result.get("is_error", result.get("error") is not None))
    return bool(getattr(result, "is_error", getattr(result, "error", None) is not None))


def _result_error(result: Any) -> Any:
    if isinstance(result, ToolExecutionResult):
        return result.error
    if isinstance(result, dict):
        return result.get("error") or result.get("output") or result.get("content")
    return getattr(result, "error", None) or getattr(result, "output", None) or getattr(result, "content", None)


def _extract_context_modifiers(result: Any) -> list[ContextModifier]:
    modifiers: Any = None
    if isinstance(result, dict):
        modifiers = result.get("context_modifiers") or result.get("context_modifier")
    else:
        modifiers = getattr(result, "context_modifiers", None) or getattr(result, "context_modifier", None)

    if modifiers is None:
        return []
    if callable(modifiers):
        return [modifiers]
    if isinstance(modifiers, list):
        return [modifier for modifier in modifiers if callable(modifier)]
    return []


def _tool_call_id(tool_call: Any) -> str:
    """Extract the id from a tool call, tolerating different shapes."""
    if hasattr(tool_call, "id"):
        return str(tool_call.id)
    if isinstance(tool_call, dict):
        return str(tool_call.get("id", ""))
    return ""


def _tool_name(tool_call: Any) -> str:
    """Extract the name from a tool call, tolerating different shapes."""
    if hasattr(tool_call, "name"):
        return str(tool_call.name)
    if isinstance(tool_call, dict):
        return str(tool_call.get("name", "<unknown>"))
    return "<unknown>"
