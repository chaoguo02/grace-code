"""Tool call partitioning by contiguous concurrency-safety class."""

from __future__ import annotations

from dataclasses import dataclass

from runtime.tool import ToolCall


@dataclass
class ToolBatch:
    """A batch of tool calls with the same concurrency-safety classification."""

    is_concurrency_safe: bool
    tool_calls: list[ToolCall]


def partition_tool_calls(
    tool_calls: list[ToolCall],
    concurrency_safe_names: set[str],
) -> list[ToolBatch]:
    """Partition tool calls by contiguous concurrency-safety status."""
    if not tool_calls:
        return []

    batches: list[ToolBatch] = []
    current_batch: list[ToolCall] = []
    current_is_safe: bool | None = None

    for tool_call in tool_calls:
        is_safe = tool_call.name in concurrency_safe_names

        if current_is_safe is not None and is_safe != current_is_safe:
            batches.append(ToolBatch(
                is_concurrency_safe=current_is_safe,
                tool_calls=current_batch,
            ))
            current_batch = []

        current_batch.append(tool_call)
        current_is_safe = is_safe

    if current_batch:
        batches.append(ToolBatch(
            is_concurrency_safe=bool(current_is_safe),
            tool_calls=current_batch,
        ))

    return batches
