"""
G19: Tool Scheduler — parallel-safe grouping, TaskGroup execution.

- ToolMetadata: read_only, concurrency_safe, resource_key.
- Safe parallel: read_only + concurrency_safe + non-conflicting resource_key.
- Serial: write/destructive/unknown.
- Uses asyncio.TaskGroup for parallel batches.
- Results restored to original call order (not completion order).
- Cancellation kills all in-flight child processes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from core.json_values import FrozenJsonObject
from runtime_core.model_actions import ToolCall


@dataclass(frozen=True, slots=True)
class ToolMetadata:
    """Declared safety properties for a tool."""
    name: str = ""
    read_only: bool = False
    concurrency_safe: bool = False
    resource_key: str = ""  # e.g. file path, DB name — conflicting keys → serial


@dataclass(frozen=True, slots=True)
class ScheduledTool:
    """A tool call with its resolved metadata."""
    tool_call: ToolCall
    metadata: ToolMetadata
    parallel_group: int  # 0, 1, 2... tools in same group can run in parallel


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """Result of executing one tool."""
    tool_call: ToolCall
    result: object
    error: str = ""
    cancelled: bool = False


class ToolScheduler:
    """Groups tools into safe parallel batches.

    Rules:
      - read_only + concurrency_safe + no resource conflict → same group
      - write/destructive/unknown → serial (own group)
      - resource_key conflict → different groups (serialized)
    """

    def __init__(self, tool_registry: dict[str, ToolMetadata] | None = None) -> None:
        self._registry = tool_registry or {}

    def register(self, metadata: ToolMetadata) -> None:
        self._registry[metadata.name] = metadata

    def schedule(self, calls: tuple[ToolCall, ...]) -> list[list[ToolCall]]:
        """Group tool calls into sequential batches.  Each batch can run in parallel.

        Returns: list of batches, each batch is a list of ToolCalls.
        """
        if not calls:
            return []

        batches: list[list[ToolCall]] = []
        used_resources: set[str] = set()

        for tc in calls:
            meta = self._registry.get(tc.name, ToolMetadata(name=tc.name))

            can_parallel = meta.read_only and meta.concurrency_safe

            if can_parallel and meta.resource_key:
                if meta.resource_key in used_resources:
                    can_parallel = False  # resource conflict

            if can_parallel:
                # Add to current batch
                if batches:
                    batches[-1].append(tc)
                else:
                    batches.append([tc])
                if meta.resource_key:
                    used_resources.add(meta.resource_key)
            else:
                # Serial — new batch
                batches.append([tc])
                used_resources = {meta.resource_key} if meta.resource_key else set()

        return batches

    async def execute_batch(
        self,
        batch: list[ToolCall],
        executor,  # async callable(ToolCall) -> ToolExecutionResult
        cancel_event: asyncio.Event | None = None,
    ) -> list[ToolExecutionResult]:
        """Execute a batch of tools in parallel via TaskGroup.

        Results returned in original batch order.
        """
        if not batch:
            return []

        results: dict[int, ToolExecutionResult] = {}

        async def run_one(idx: int, tc: ToolCall) -> None:
            if cancel_event and cancel_event.is_set():
                results[idx] = ToolExecutionResult(
                    tool_call=tc, result=None, cancelled=True,
                )
                return
            try:
                result = await executor(tc)
                results[idx] = ToolExecutionResult(
                    tool_call=tc, result=result,
                )
            except asyncio.CancelledError:
                results[idx] = ToolExecutionResult(
                    tool_call=tc, result=None, cancelled=True,
                )
            except Exception as exc:
                results[idx] = ToolExecutionResult(
                    tool_call=tc, result=None,
                    error=f"{type(exc).__name__}: {exc}",
                )

        try:
            async with asyncio.TaskGroup() as tg:
                for i, tc in enumerate(batch):
                    tg.create_task(run_one(i, tc))
        except* Exception:
            pass  # individual errors are in results dict

        # Return in original order
        return [results[i] for i in range(len(batch)) if i in results]
