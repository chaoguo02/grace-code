"""Streaming Tool Executor — CC-aligned tool orchestration for BaseTool system.

Aligns with Claude Code's StreamingToolExecutor:
  - Per-call concurrency safety (not per-tool-type)
  - Admission control (mutual exclusion — non-safe tool blocks all others)
  - Order-preserving result yield (input order, not completion order)
  - Tool lifecycle tracking (queued → executing → completed → yielded)
  - Sibling abort controller (Bash error → cancel concurrent siblings)
  - Partition algorithm: consecutive safe tools → batch, non-safe → serial

Unlike executor/tool_executor.py (which targets the Protocol-based executor/tool.py
system), this module integrates directly with core/base.py::BaseTool and
core/base.py::ToolRegistry — the actual tool system used by the ReAct agent loop.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, TYPE_CHECKING

from core.base import ToolConcurrency, ToolErrorType, ToolResult

if TYPE_CHECKING:
    from core.base import ToolRegistry
    from agent.task import ToolCall

logger = logging.getLogger(__name__)


# ── Lifecycle ────────────────────────────────────────────────────────────────

class TrackedStatus(str, Enum):
    QUEUED = "queued"
    EXECUTING = "executing"
    COMPLETED = "completed"
    YIELDED = "yielded"
    CANCELLED = "cancelled"


@dataclass
class TrackedTool:
    """One tool call in the executor's tracking queue."""
    tool_call: "ToolCall"
    status: TrackedStatus = TrackedStatus.QUEUED
    result: ToolResult | None = None
    error: str | None = None
    started_at: float = 0.0
    finished_at: float = 0.0
    future: Any = None  # concurrent.futures.Future | None

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            TrackedStatus.COMPLETED,
            TrackedStatus.YIELDED,
            TrackedStatus.CANCELLED,
        )

    @property
    def duration_ms(self) -> float:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at) * 1000
        return 0.0


# ── Abort Controller ─────────────────────────────────────────────────────────

class SiblingAbortController:
    """Per-batch abort signal for sibling tool cancellation.

    CC behaviour: when Bash errors, the executor cancels all concurrently-running
    tools (the sibling controller).  Read/Grep errors do NOT cancel siblings.
    """

    def __init__(self) -> None:
        self._aborted = threading.Event()
        self._reason: str = ""

    @property
    def is_aborted(self) -> bool:
        return self._aborted.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    def abort(self, reason: str) -> None:
        self._reason = reason
        self._aborted.set()


# ── Partition Algorithm ──────────────────────────────────────────────────────

def _is_concurrency_safe(
    tool_call: "ToolCall",
    registry: "ToolRegistry",
) -> bool:
    """Per-call concurrency safety check for a single tool call.

    Wraps registry.concurrency_for() which delegates to BaseTool.concurrency_mode().
    The check is fail-closed: any exception defaults to serial.
    """
    try:
        return (
            registry.concurrency_for(tool_call.name, tool_call.params or {})
            is ToolConcurrency.PARALLEL_SAFE
        )
    except Exception:
        return False


def partition_tool_calls(
    tool_calls: list["ToolCall"],
    registry: "ToolRegistry",
) -> list[list["ToolCall"]]:
    """CC-aligned partition: consecutive safe calls form a batch, non-safe break.

    Example: [Read, Grep, Bash(ls), Edit, Read]
      → Batch1[Read, Grep, Bash(ls)], Batch2[Edit], Batch3[Read]

    The partition preserves input order.  Each batch is either fully concurrent
    (all calls concurrency-safe) or a single serial entry.
    """
    if not tool_calls:
        return []

    batches: list[list["ToolCall"]] = []
    for call in tool_calls:
        safe = _is_concurrency_safe(call, registry)
        if safe and batches and len(batches[-1]) > 0:
            # Check if the last batch's first call was also safe
            first_of_last = batches[-1][0]
            if _is_concurrency_safe(first_of_last, registry):
                batches[-1].append(call)
                continue
        batches.append([call])
    return batches


# ── Streaming Tool Executor ──────────────────────────────────────────────────

@dataclass
class ExecutorConfig:
    """Configuration for StreamingToolExecutor."""
    max_workers: int = 10
    """Maximum concurrent tool executions (CC default: 10)."""
    abort_on_bash_error: bool = True
    """Cancel sibling tools when Bash returns non-zero exit code."""


class StreamingToolExecutor:
    """CC-aligned streaming tool executor for the BaseTool system.

    Usage in the agent loop::

        executor = StreamingToolExecutor(registry)
        for tool_call in model_response.tool_calls:
            executor.enqueue(tool_call)
        executor.dispatch()            # start all queued tools (respecting batches)
        results = executor.collect()   # get results in input order
    """

    def __init__(
        self,
        registry: "ToolRegistry",
        config: ExecutorConfig | None = None,
    ) -> None:
        self._registry = registry
        self._config = config or ExecutorConfig()
        self._tracked: list[TrackedTool] = []
        self._sibling_abort = SiblingAbortController()
        self._lock = threading.Lock()

    # ── Queue management ─────────────────────────────────────────────────

    def enqueue(self, tool_call: "ToolCall") -> None:
        """Register a newly parsed tool_use block for execution."""
        tracked = TrackedTool(tool_call=tool_call)
        with self._lock:
            self._tracked.append(tracked)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return sum(
                1 for t in self._tracked
                if t.status in (TrackedStatus.QUEUED, TrackedStatus.EXECUTING)
            )

    # ── Dispatch ─────────────────────────────────────────────────────────

    def dispatch(self) -> None:
        """Partition and start executing all queued tools.

        Uses partition_tool_calls to batch concurrent-safe calls together.
        Within each batch, tools execute in a thread pool.  Between batches,
        execution is serial (each batch waits for the previous one).
        """
        with self._lock:
            queued = [t for t in self._tracked if t.status == TrackedStatus.QUEUED]
            if not queued:
                return

        calls = [t.tool_call for t in queued]
        batches = partition_tool_calls(calls, self._registry)

        for batch in batches:
            if self._sibling_abort.is_aborted:
                self._cancel_queued(self._sibling_abort.reason)
                return

            if len(batch) == 1:
                self._execute_serial(batch[0])
            else:
                self._execute_concurrent(batch)

    def _execute_serial(self, tool_call: "ToolCall") -> None:
        tracked = self._find_tracked(tool_call)
        if tracked is None:
            return
        self._run_one(tracked)

    def _execute_concurrent(self, tool_calls: list["ToolCall"]) -> None:
        """Execute a batch of concurrency-safe tools in parallel."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        batch_tracked = []
        for tc in tool_calls:
            t = self._find_tracked(tc)
            if t is not None:
                batch_tracked.append(t)

        if not batch_tracked:
            return

        max_w = min(len(batch_tracked), self._config.max_workers)
        with ThreadPoolExecutor(
            max_workers=max_w, thread_name_prefix="forge-stream"
        ) as pool:
            futures = {}
            for t in batch_tracked:
                with self._lock:
                    t.status = TrackedStatus.EXECUTING
                    t.started_at = time.monotonic()
                fut = pool.submit(self._execute_one, t)
                futures[fut] = t

            for fut in as_completed(futures):
                t = futures[fut]
                try:
                    fut.result()
                except Exception as exc:
                    with self._lock:
                        t.error = str(exc)
                        t.status = TrackedStatus.COMPLETED
                        t.finished_at = time.monotonic()
                    # Bash error → cancel siblings
                    if self._config.abort_on_bash_error and t.tool_call.name == "Bash":
                        reason = f"Cancelled: parallel tool call Bash errored — {t.error or 'exit non-zero'}"
                        self._sibling_abort.abort(reason)
                        self._cancel_executing(reason)

    def _run_one(self, tracked: TrackedTool) -> None:
        """Execute a single tool synchronously (for serial batches)."""
        with self._lock:
            tracked.status = TrackedStatus.EXECUTING
            tracked.started_at = time.monotonic()
        self._execute_one(tracked)

    def _execute_one(self, tracked: TrackedTool) -> None:
        """Execute one tool and store the result.  Runs on a worker thread."""
        tc = tracked.tool_call
        try:
            result = self._registry.execute_tool(tc.name, tc.params or {})
            with self._lock:
                tracked.result = result
                tracked.status = TrackedStatus.COMPLETED
                tracked.finished_at = time.monotonic()
        except Exception as exc:
            with self._lock:
                tracked.error = str(exc)
                tracked.status = TrackedStatus.COMPLETED
                tracked.finished_at = time.monotonic()

    # ── Collect ──────────────────────────────────────────────────────────

    def collect(self) -> list[ToolResult]:
        """Return all tool results in input order (order-preserving yield).

        Blocks until all queued + executing tools have completed.
        After collection, all tracked entries transition to YIELDED.
        """
        # Wait for all executing tools to finish
        while True:
            with self._lock:
                pending = sum(
                    1 for t in self._tracked
                    if t.status in (TrackedStatus.QUEUED, TrackedStatus.EXECUTING)
                )
            if pending == 0:
                break
            time.sleep(0.01)

        results: list[ToolResult] = []
        with self._lock:
            for t in self._tracked:
                if t.status == TrackedStatus.COMPLETED:
                    if t.result is not None:
                        results.append(t.result)
                    elif t.error:
                        results.append(ToolResult.from_error(
                        ToolErrorType.INTERNAL, detail=t.error or "Tool error",
                    ))
                    t.status = TrackedStatus.YIELDED
                elif t.status == TrackedStatus.CANCELLED:
                    results.append(ToolResult.from_error(
                        ToolErrorType.INTERNAL, detail=t.error or "Tool cancelled",
                    ))
                    t.status = TrackedStatus.YIELDED
        return results

    def collect_with_observations(
        self, build_observation: Callable[["ToolCall", ToolResult], Any]
    ) -> list[Any]:
        """Collect results and convert to observations in input order."""
        results = self.collect()
        observations = []
        for t in self._tracked:
            if t.status == TrackedStatus.YIELDED and t.result is not None:
                observations.append(build_observation(t.tool_call, t.result))
            elif t.status == TrackedStatus.YIELDED and t.error:
                fake_result = ToolResult.from_error(
                    ToolErrorType.INTERNAL, detail=t.error or "Tool error",
                )
                observations.append(build_observation(t.tool_call, fake_result))
        return observations

    # ── Cancellation ─────────────────────────────────────────────────────

    def abort_all(self, reason: str = "Executor aborted") -> None:
        """Abort all queued and executing tools."""
        self._sibling_abort.abort(reason)
        self._cancel_all(reason)

    def _cancel_queued(self, reason: str) -> None:
        with self._lock:
            for t in self._tracked:
                if t.status == TrackedStatus.QUEUED:
                    t.status = TrackedStatus.CANCELLED
                    t.error = reason

    def _cancel_executing(self, reason: str) -> None:
        with self._lock:
            for t in self._tracked:
                if t.status == TrackedStatus.EXECUTING:
                    t.status = TrackedStatus.CANCELLED
                    t.error = reason

    def _cancel_all(self, reason: str) -> None:
        with self._lock:
            for t in self._tracked:
                if t.status in (TrackedStatus.QUEUED, TrackedStatus.EXECUTING):
                    t.status = TrackedStatus.CANCELLED
                    t.error = reason

    # ── Helpers ──────────────────────────────────────────────────────────

    def _find_tracked(self, tool_call: "ToolCall") -> TrackedTool | None:
        with self._lock:
            for t in self._tracked:
                if t.tool_call is tool_call:
                    return t
        return None

    @property
    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = len(self._tracked)
            status_counts = {}
            for t in self._tracked:
                status_counts[t.status.value] = status_counts.get(t.status.value, 0) + 1
            durations = [t.duration_ms for t in self._tracked if t.duration_ms > 0]
            return {
                "total": total,
                "statuses": status_counts,
                "total_duration_ms": sum(durations),
                "max_duration_ms": max(durations) if durations else 0,
            }
