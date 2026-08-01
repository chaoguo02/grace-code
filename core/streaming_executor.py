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
        if safe and batches and _is_concurrency_safe(batches[-1][0], registry):
            # Merge into current concurrent batch (CC: acc[-1].isConcurrencySafe)
            batches[-1].append(call)
        else:
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
        # Shared thread pool for speculative execution (avoids per-call pool leak)
        self._pool = None
        self._pool_lock = threading.Lock()
        # CC-aligned: event-driven wake signal for collect() instead of polling.
        # Set whenever a tool completes or is cancelled.
        self._wake = threading.Event()
        # Optional: process registry for active cancellation (P0_3 Batch 1).
        # When set, _cancel_executing() will kill tracked subprocesses.
        self._process_registry: object | None = None  # ProcessRegistry | None
        # Optional: CancellationHandle for pipeline propagation (P0_3 Batch 2).
        self._cancellation_handle: object | None = None
        # Track active futures so _cancel_executing can .cancel() them.
        self._active_futures: dict[str, object] = {}  # invocation_id → Future
        # P2: observation cursor — prevents duplicate injection
        self._last_observed_count: int = 0

    # ── Queue management ─────────────────────────────────────────────────

    def _dedup_key(self, tool_call: "ToolCall") -> str:
        """Stable dedup key: tool_call.id if present, else hash(name + params)."""
        if tool_call.id:
            return str(tool_call.id)
        import hashlib, json
        return hashlib.md5(
            f"{tool_call.name}:{json.dumps(tool_call.params or {}, sort_keys=True)}".encode()
        ).hexdigest()[:20]

    def enqueue(self, tool_call: "ToolCall") -> None:
        """Register a newly parsed tool_use block AND try to start it immediately.

        CC-aligned: tool_use blocks can execute while the model is still
        generating text (speculative execution).  If admission control allows,
        the tool starts on a worker thread before dispatch() is called.

        Idempotent: duplicate tool_call_id (or name+params hash fallback)
        is silently skipped.  This prevents double execution.
        """
        key = self._dedup_key(tool_call)
        with self._lock:
            for t in self._tracked:
                if self._dedup_key(t.tool_call) == key:
                    return  # already registered
        tracked = TrackedTool(tool_call=tool_call)
        with self._lock:
            self._tracked.append(tracked)
        # Try speculative start — if concurrency allows, runs immediately
        self._try_start(tracked)

    # ── Admission Control ───────────────────────────────────────────────

    def _try_start(self, tracked: TrackedTool) -> bool:
        """Start *tracked* if mutual-exclusion rules allow. Returns True if started.

        CC-aligned admission control:
          - Non-safe tool needs exclusive access (nothing else executing)
          - Safe tool can share with other safe tools, but NOT with non-safe
          - A single non-safe tool blocks all other tools (safe and non-safe)
        """
        with self._lock:
            if tracked.status != TrackedStatus.QUEUED:
                return False
            safe = _is_concurrency_safe(tracked.tool_call, self._registry)
            executing = [
                t for t in self._tracked
                if t.status == TrackedStatus.EXECUTING
            ]
            if executing:
                # Something is running — check mutual exclusion
                any_non_safe = any(
                    not _is_concurrency_safe(t.tool_call, self._registry)
                    for t in executing
                )
                if any_non_safe:
                    # A non-safe tool owns the runway — nothing else can start
                    return False
                if not safe:
                    # This tool is non-safe and others are running (but all safe)
                    # Non-safe tool needs exclusive access
                    return False
            # Start immediately
            tracked.status = TrackedStatus.EXECUTING
            tracked.started_at = time.monotonic()
        # Submit to shared thread pool (lazy init, avoids per-call pool leak)
        if self._pool is None:
            with self._pool_lock:
                if self._pool is None:
                    from concurrent.futures import ThreadPoolExecutor
                    self._pool = ThreadPoolExecutor(
                        max_workers=self._config.max_workers,
                        thread_name_prefix="grace-spec",
                    )
        tracked.future = self._pool.submit(self._execute_one, tracked)
        return True

    def process_queue(self) -> int:
        """Scan queued tools and start any that can now run. Returns started count."""
        started = 0
        with self._lock:
            queued = [t for t in self._tracked if t.status == TrackedStatus.QUEUED]
        for t in queued:
            if self._try_start(t):
                started += 1
        return started

    @property
    def pending_count(self) -> int:
        with self._lock:
            return sum(
                1 for t in self._tracked
                if t.status in (TrackedStatus.QUEUED, TrackedStatus.EXECUTING)
            )

    @property
    def completed_count(self) -> int:
        """Number of completed tools not yet yielded (non-blocking)."""
        with self._lock:
            return sum(1 for t in self._tracked if t.status == TrackedStatus.COMPLETED)

    def get_completed_results(self) -> list[ToolResult]:
        """Non-blocking: return results for tools that finished since last call.

        CC-aligned mid-stream yield.  Call this between stream_iter events to
        collect early results without blocking on still-executing tools.
        Results are yielded in input order; each tool is yielded at most once.
        """
        results: list[ToolResult] = []
        with self._lock:
            for t in self._tracked:
                if t.status == TrackedStatus.COMPLETED:
                    if t.result is not None:
                        results.append(t.result)
                    elif t.error:
                        results.append(ToolResult.from_error(
                            ToolErrorType.INTERNAL, detail=t.error,
                        ))
                    t.status = TrackedStatus.YIELDED
        return results

    # ── Dispatch ─────────────────────────────────────────────────────────

    def dispatch(self) -> None:
        """Start all remaining queued tools (post-stream drain).

        Tools may already be executing from speculative starts (enqueue → _try_start).
        This method just starts whatever is left in the queue.
        """
        # Keep trying to start queued tools until no more can start
        # (some may be blocked by executing non-safe tools)
        while True:
            started = self.process_queue()
            if started == 0:
                break
            # Give started tools a chance to complete, unblocking the queue
            if self.pending_count > 0:
                time.sleep(0.01)

    def _execute_one(self, tracked: TrackedTool) -> None:
        """Execute one tool and store the result.  Runs on a worker thread.

        CC-aligned Bash error cascade: when Bash returns non-zero, the
        siblingAbortController cancels all concurrently-running tools.
        This prevents cascading failures (e.g. mkdir fails → cp doomed).
        Read/Grep errors do NOT cancel siblings — their failures are independent.

        After completion, calls process_queue() to unblock tools that were
        waiting for exclusive access (serial tools blocked by concurrent batch).
        """
        tc = tracked.tool_call
        try:
            result = self._registry.execute_tool(
                tc.name,
                tc.params or {},
                invocation_id=tc.id or "",
                cancel_token=getattr(self, "_cancellation_handle", None),
            )
            # P0_3 CAS: only transition to COMPLETED if not already CANCELLED.
            # A concurrent _cancel_executing() may have set CANCELLED while
            # the tool was executing.  Reject completed → cancelled overwrite.
            with self._lock:
                if tracked.status != TrackedStatus.CANCELLED:
                    tracked.result = result
                    tracked.status = TrackedStatus.COMPLETED
                    tracked.finished_at = time.monotonic()
                self._wake.set()
            # CC: Bash error → cancel sibling parallel tools
            if (
                self._config.abort_on_bash_error
                and tc.name == "Bash"
                and not result.success
            ):
                reason = (
                    f"Cancelled: parallel tool call Bash errored — "
                    f"{result.error or 'exit non-zero'}"
                )
                self._sibling_abort.abort(reason)
                self._cancel_executing(reason)
            # Unblock tools waiting for exclusive access (e.g. serial after parallel)
            self.process_queue()
        except Exception as exc:
            with self._lock:
                # CAS: error can override EXECUTING, but NOT CANCELLED
                if tracked.status != TrackedStatus.CANCELLED:
                    tracked.error = str(exc)
                    tracked.status = TrackedStatus.COMPLETED
                    tracked.finished_at = time.monotonic()
                self._wake.set()
            self.process_queue()

    # ── Collect ──────────────────────────────────────────────────────────

    def collect(self) -> list[ToolResult]:
        """Return all tool results in input order (order-preserving yield).

        Blocks until all queued + executing tools have completed.
        Uses event-driven wake (CC: progressAvailableResolve) instead of polling.
        After collection, all tracked entries transition to YIELDED.
        """
        # CC-aligned: wait with wake events instead of sleep polling
        while True:
            with self._lock:
                pending = sum(
                    1 for t in self._tracked
                    if t.status in (TrackedStatus.QUEUED, TrackedStatus.EXECUTING)
                )
                if pending == 0:
                    break
            # Wait for a tool completion wake signal (CC: progressAvailableResolve)
            self._wake.wait(timeout=5.0)
            self._wake.clear()

        results: list[ToolResult] = []
        with self._lock:
            for t in self._tracked:
                if t.status == TrackedStatus.YIELDED:
                    continue  # already yielded by get_completed_results()
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
        """Collect results and convert to observations in input order.

        P2: Returns only NEW results since the last call (cursor-based).
        Prevents duplicate observation injection on repeated calls.
        """
        new_results = self.collect()
        observations = []
        yielded_count = 0
        for t in self._tracked:
            if t.status != TrackedStatus.YIELDED:
                continue
            yielded_count += 1
            # P2: skip already-observed results
            if yielded_count <= self._last_observed_count:
                continue
            if t.result is not None:
                observations.append(build_observation(t.tool_call, t.result))
            elif t.error:
                fake_result = ToolResult.from_error(
                    ToolErrorType.INTERNAL, detail=t.error or "Tool error",
                )
                observations.append(build_observation(t.tool_call, fake_result))
        self._last_observed_count = yielded_count
        return observations

    def set_process_registry(self, registry: object) -> None:
        """Attach a ProcessRegistry for active cancellation (P0_3 Batch 1).

        When set, _cancel_executing() will kill subprocesses tracked
        by the registry in addition to updating status fields.
        """
        self._process_registry = registry

    def set_cancellation_handle(self, handle: object) -> None:
        """Attach a CancellationHandle for pipeline propagation (P0_3 Batch 2).

        When set, _execute_one() forwards it to ToolRegistry.execute_tool()
        → ToolExecutionPipeline → ResourceGovernor, so queued tools are
        released immediately on cancellation.
        """
        self._cancellation_handle = handle

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
        """Cancel all EXECUTING tools + kill their subprocesses.

        P0_3 Batch 1: when _process_registry is attached, actively kills
        the subprocesses tracked for each cancelled invocation.  Also
        attempts future.cancel() on active worker futures.
        """
        with self._lock:
            for t in self._tracked:
                if t.status == TrackedStatus.EXECUTING:
                    t.status = TrackedStatus.CANCELLED
                    t.error = reason
            self._wake.set()

        # P0_3: kill registered subprocesses for cancelled invocations
        registry = self._process_registry
        if registry is not None:
            for t in self._tracked:
                if t.status == TrackedStatus.CANCELLED and t.tool_call.id:
                    try:
                        registry.kill_one(t.tool_call.id, escalate=True)
                    except Exception:
                        pass

        # P0_3: cancel active ThreadPoolExecutor futures
        for t in self._tracked:
            if t.status == TrackedStatus.CANCELLED:
                fut = self._active_futures.pop(t.tool_call.id or "", None)
                if fut is not None:
                    try:
                        fut.cancel()
                    except Exception:
                        pass

    def _cancel_all(self, reason: str) -> None:
        with self._lock:
            for t in self._tracked:
                if t.status in (TrackedStatus.QUEUED, TrackedStatus.EXECUTING):
                    t.status = TrackedStatus.CANCELLED
                    t.error = reason
            self._wake.set()

    # ── Lifecycle (Phase 2) ─────────────────────────────────────────────

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = True) -> None:
        """Shutdown the internal thread pool and cancel pending work.

        Safe to call multiple times (idempotent).
        """
        self.abort_all("Executor shutdown")
        if self._pool is not None:
            self._pool.shutdown(wait=wait, cancel_futures=cancel_futures)
            self._pool = None

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
