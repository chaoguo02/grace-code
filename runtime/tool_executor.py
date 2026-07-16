"""
Tool Executor — aligned with Claude Code tool orchestration.

Source basis:
  E: validateInput → checkPermissions → call → renderResult
  F: partitionToolCalls batching
  G: StreamingToolExecutor
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, TypeVar

from runtime.tool import PermissionDecision, ToolCall, ToolExecutionResult, ToolUseContext

if TYPE_CHECKING:
    from runtime.tool import ConcreteTool
    from runtime.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)

ItemT = TypeVar("ItemT")
ResultT = TypeVar("ResultT")


def execute_parallel_sync(
    items: list[ItemT],
    execute: Callable[[ItemT], ResultT],
    *,
    max_workers: int,
) -> list[ResultT]:
    """Execute a Runtime-approved synchronous batch and preserve input order."""
    if max_workers < 1:
        raise ValueError("max_workers must be positive")
    if len(items) < 2:
        return [execute(item) for item in items]
    with ThreadPoolExecutor(
        max_workers=min(len(items), max_workers),
        thread_name_prefix="forge-tool",
    ) as executor:
        return list(executor.map(execute, items))


async def execute_single_tool(
    tool: ConcreteTool,
    call: ToolCall,
    context: ToolUseContext,
) -> ToolExecutionResult:
    """Execute one tool through validation, permission, and call stages."""
    started_at = time.monotonic()

    try:
        is_valid, error_msg = await tool.validate_input(call.input, context)
        if not is_valid:
            return ToolExecutionResult(
                call_id=call.id,
                tool_name=call.name,
                error=f"Input validation failed: {error_msg}",
                duration_ms=_elapsed_ms(started_at),
                started_at=started_at,
            )

        permission = await tool.check_permissions(call.input, context)
        if permission == PermissionDecision.DENY:
            return ToolExecutionResult(
                call_id=call.id,
                tool_name=call.name,
                error="Permission denied",
                duration_ms=_elapsed_ms(started_at),
                started_at=started_at,
            )
        if permission == PermissionDecision.ASK:
            return ToolExecutionResult(
                call_id=call.id,
                tool_name=call.name,
                error="Permission required but no interactive UI available",
                duration_ms=_elapsed_ms(started_at),
                started_at=started_at,
            )

        result = await tool.call(call.input, context)
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            result=result,
            duration_ms=_elapsed_ms(started_at),
            started_at=started_at,
        )

    except Exception as exc:
        logger.exception("Tool %s execution failed", call.name)
        return ToolExecutionResult(
            call_id=call.id,
            tool_name=call.name,
            error=str(exc),
            duration_ms=_elapsed_ms(started_at),
            started_at=started_at,
        )


@dataclass
class Batch:
    """Tool call batch: safe batches can run concurrently, unsafe batches run serially."""

    is_concurrency_safe: bool
    calls: list[ToolCall] = field(default_factory=list)


def partition_tool_calls(calls: list[ToolCall], registry: ToolRegistry) -> list[Batch]:
    """Partition tool calls into concurrency-safe contiguous batches."""
    if not calls:
        return []

    batches: list[Batch] = []
    for call in calls:
        tool = registry.find_by_name(call.name)
        is_safe = tool.is_concurrency_safe(call.input) if tool else False

        if is_safe and batches and batches[-1].is_concurrency_safe:
            batches[-1].calls.append(call)
        else:
            batches.append(Batch(is_concurrency_safe=is_safe, calls=[call]))

    return batches


async def execute_batch(
    batch: Batch,
    registry: ToolRegistry,
    context: ToolUseContext,
) -> list[ToolExecutionResult]:
    """Execute one batch concurrently if safe, otherwise serially."""
    if batch.is_concurrency_safe and len(batch.calls) > 1:
        tasks = []
        for call in batch.calls:
            tool = registry.find_by_name(call.name)
            if tool:
                tasks.append(execute_single_tool(tool, call, context))
            else:
                tasks.append(_unknown_tool_result(call))
        return await asyncio.gather(*tasks)

    results: list[ToolExecutionResult] = []
    for call in batch.calls:
        tool = registry.find_by_name(call.name)
        if tool:
            result = await execute_single_tool(tool, call, context)
        else:
            result = await _unknown_tool_result(call)
        results.append(result)
    return results


async def execute_tool_calls(
    calls: list[ToolCall],
    registry: ToolRegistry,
    context: ToolUseContext,
) -> list[ToolExecutionResult]:
    """Partition and execute model tool calls."""
    batches = partition_tool_calls(calls, registry)
    all_results: list[ToolExecutionResult] = []

    for batch in batches:
        batch_results = await execute_batch(batch, registry, context)
        all_results.extend(batch_results)

    return all_results


class StreamingToolExecutor:
    """Simplified streaming tool executor."""

    def __init__(self, registry: ToolRegistry, context: ToolUseContext) -> None:
        self._registry = registry
        self._context = context
        self._pending_calls: list[ToolCall] = []
        self._results: list[ToolExecutionResult] = []

    def add_tool(self, call: ToolCall) -> None:
        """Add a parsed tool call to the pending execution queue."""
        self._pending_calls.append(call)

    async def execute_all(self) -> list[ToolExecutionResult]:
        """Execute all pending tool calls and clear the pending queue."""
        if not self._pending_calls:
            return []

        results = await execute_tool_calls(
            calls=self._pending_calls,
            registry=self._registry,
            context=self._context,
        )
        self._results.extend(results)
        self._pending_calls.clear()
        return results

    @property
    def has_pending(self) -> bool:
        return len(self._pending_calls) > 0

    @property
    def all_results(self) -> list[ToolExecutionResult]:
        return list(self._results)


def _elapsed_ms(started_at: float) -> float:
    return (time.monotonic() - started_at) * 1000


async def _unknown_tool_result(call: ToolCall) -> ToolExecutionResult:
    return ToolExecutionResult(
        call_id=call.id,
        tool_name=call.name,
        error=f"Unknown tool: {call.name}",
    )
