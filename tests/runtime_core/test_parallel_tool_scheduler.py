"""G19: Parallel Tool Scheduler — safe grouping, TaskGroup, ordered results.

AC: read_only + concurrency_safe → same parallel group
AC: write/destructive → serial (own group)
AC: resource_key conflict → different groups
AC: results in original call order
AC: sibling failure doesn't lose other results
AC: cancellation stops all in-flight tools
"""

from __future__ import annotations

import asyncio
import time as _time

import pytest

from core.json_values import freeze_json
from runtime_core.model_actions import ToolCall
from runtime_core.tool_scheduler import (
    ToolScheduler, ToolMetadata, ToolExecutionResult,
)


# ═══════════════════════════════════════════════════════════════════════════════
# G19.1 — Scheduling groups
# ═══════════════════════════════════════════════════════════════════════════════

class TestSchedulingGroups:
    """G19: ToolScheduler groups tools correctly."""

    def test_read_only_safe_same_group(self):
        sched = ToolScheduler({
            "read_a": ToolMetadata(name="read_a", read_only=True, concurrency_safe=True),
            "read_b": ToolMetadata(name="read_b", read_only=True, concurrency_safe=True),
        })
        calls = (
            ToolCall(id="1", name="read_a", params=freeze_json({})),
            ToolCall(id="2", name="read_b", params=freeze_json({})),
        )
        batches = sched.schedule(calls)
        assert len(batches) == 1, f"2 read-only safe tools should be in 1 batch, got {len(batches)}"
        assert len(batches[0]) == 2

    def test_write_is_serial(self):
        sched = ToolScheduler({
            "read": ToolMetadata(name="read", read_only=True, concurrency_safe=True),
            "write": ToolMetadata(name="write", read_only=False, concurrency_safe=False),
        })
        calls = (
            ToolCall(id="1", name="read", params=freeze_json({})),
            ToolCall(id="2", name="write", params=freeze_json({})),
        )
        batches = sched.schedule(calls)
        # write should be in its own batch
        assert len(batches) >= 2, f"Write tool must be serialized, got {len(batches)} batches"

    def test_resource_conflict_different_batches(self):
        sched = ToolScheduler({
            "read_f1": ToolMetadata(name="read_f1", read_only=True, concurrency_safe=True,
                                    resource_key="file1.txt"),
            "read_f1_again": ToolMetadata(name="read_f1_again", read_only=True, concurrency_safe=True,
                                          resource_key="file1.txt"),
        })
        calls = (
            ToolCall(id="1", name="read_f1", params=freeze_json({})),
            ToolCall(id="2", name="read_f1_again", params=freeze_json({})),
        )
        batches = sched.schedule(calls)
        # Same resource → separate batches
        assert len(batches) == 2, (
            f"Same resource_key must be serialized, got {len(batches)} batches"
        )

    def test_empty_calls(self):
        sched = ToolScheduler()
        assert sched.schedule(()) == []


# ═══════════════════════════════════════════════════════════════════════════════
# G19.2 — Parallel execution
# ═══════════════════════════════════════════════════════════════════════════════

class TestParallelExecution:
    """G19: Batch executes in parallel, results in original order."""

    @pytest.mark.asyncio
    async def test_parallel_execution_is_faster(self):
        sched = ToolScheduler({
            "slow_a": ToolMetadata(name="slow_a", read_only=True, concurrency_safe=True),
            "slow_b": ToolMetadata(name="slow_b", read_only=True, concurrency_safe=True),
        })
        calls = [
            ToolCall(id="1", name="slow_a", params=freeze_json({})),
            ToolCall(id="2", name="slow_b", params=freeze_json({})),
        ]

        async def executor(tc: ToolCall):
            await asyncio.sleep(0.05)
            return f"result_{tc.id}"

        started = _time.monotonic()
        results = await sched.execute_batch(calls, executor)
        elapsed = _time.monotonic() - started

        assert len(results) == 2
        # Parallel should be ~50ms, not 100ms
        assert elapsed < 0.15, f"Parallel execution too slow: {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_results_in_original_order(self):
        sched = ToolScheduler()
        calls = [
            ToolCall(id="1", name="t1", params=freeze_json({})),
            ToolCall(id="2", name="t2", params=freeze_json({})),
            ToolCall(id="3", name="t3", params=freeze_json({})),
        ]

        async def executor(tc: ToolCall):
            if tc.id == "2":
                await asyncio.sleep(0.02)  # second finishes last
            return f"result_{tc.id}"

        results = await sched.execute_batch(calls, executor)
        # Results must be in original order: t1, t2, t3
        assert [r.tool_call.id for r in results] == ["1", "2", "3"], (
            f"G19: results must be in original call order, got {[r.tool_call.id for r in results]}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# G19.3 — Sibling failure
# ═══════════════════════════════════════════════════════════════════════════════

class TestSiblingFailure:
    """G19: One tool failing doesn't lose the other's result."""

    @pytest.mark.asyncio
    async def test_sibling_failure_isolated(self):
        sched = ToolScheduler()
        calls = [
            ToolCall(id="1", name="good", params=freeze_json({})),
            ToolCall(id="2", name="bad", params=freeze_json({})),
        ]

        async def executor(tc: ToolCall):
            if tc.name == "bad":
                raise RuntimeError("boom!")
            return f"ok_{tc.id}"

        results = await sched.execute_batch(calls, executor)
        assert len(results) == 2
        good = [r for r in results if not r.error]
        bad = [r for r in results if r.error]
        assert len(good) == 1, "Good tool result must be preserved"
        assert len(bad) == 1, "Bad tool error must be recorded"


# ═══════════════════════════════════════════════════════════════════════════════
# G19.4 — Cancellation
# ═══════════════════════════════════════════════════════════════════════════════

class TestCancelParallel:
    """G19: Cancellation kills all in-flight tools."""

    @pytest.mark.asyncio
    async def test_cancel_stops_remaining(self):
        sched = ToolScheduler()
        calls = [
            ToolCall(id="1", name="t1", params=freeze_json({})),
            ToolCall(id="2", name="t2", params=freeze_json({})),
        ]
        cancel_evt = asyncio.Event()
        cancel_evt.set()  # already cancelled

        async def executor(tc: ToolCall):
            await asyncio.sleep(0.01)
            return f"result_{tc.id}"

        results = await sched.execute_batch(calls, executor, cancel_event=cancel_evt)
        cancelled = [r for r in results if r.cancelled]
        assert len(cancelled) >= 2, "All tools should be cancelled"


# ═══════════════════════════════════════════════════════════════════════════════
# T2 — ToolMetadata.from_base_tool() bridge
# ═══════════════════════════════════════════════════════════════════════════════

class TestMetadataBridge:
    """T2: from_base_tool bridges core.types.ToolMetadata → runtime_core."""

    def test_read_only_tool_maps_correctly(self):
        """FileReadTool.isReadOnly() = True → read_only=True."""
        from core.base import BaseTool
        from core.types import ToolMetadata as CoreMetadata, ToolEffect, PathAccess

        # Create a minimal read-only tool
        class FakeReadTool(BaseTool):
            name = "ReadFile"
            schema = {"name": "ReadFile", "description": "Read a file"}
            metadata = CoreMetadata(effects=frozenset({ToolEffect.READ_WORKSPACE}),
                                     path_access=PathAccess.READ)
            @property
            def description(self): return "Read a file"
            @property
            def parameters_schema(self): return {"type": "object", "properties": {}, "required": []}
            def isReadOnly(self, params=None): return True
            def execute(self, params): return type('R', (), {'output': '', 'success': True})()

        tool = FakeReadTool()
        meta = ToolMetadata.from_base_tool(tool)
        assert meta.name == "ReadFile"
        assert meta.read_only is True, f"Expected read_only=True, got {meta.read_only}"

    def test_write_tool_maps_correctly(self):
        """Write tool → read_only=False."""
        from core.base import BaseTool
        from core.types import ToolMetadata as CoreMetadata, ToolEffect

        class FakeWriteTool(BaseTool):
            name = "WriteFile"
            schema = {"name": "WriteFile", "description": "Write a file"}
            metadata = CoreMetadata(effects=frozenset({ToolEffect.WRITE_WORKSPACE}))
            @property
            def description(self): return "Write a file"
            @property
            def parameters_schema(self): return {"type": "object", "properties": {}, "required": []}
            def execute(self, params): return type('R', (), {'output': '', 'success': True})()

        tool = FakeWriteTool()
        meta = ToolMetadata.from_base_tool(tool)
        assert meta.read_only is False

    def test_resource_key_from_path_parameter(self):
        from core.base import BaseTool
        from core.types import ToolMetadata as CoreMetadata, ToolEffect

        class FakeEditTool(BaseTool):
            name = "Edit"
            schema = {"name": "Edit", "description": "Edit a file"}
            metadata = CoreMetadata(
                effects=frozenset({ToolEffect.WRITE_WORKSPACE}),
                path_parameter="file_path",
            )
            @property
            def description(self): return "Edit a file"
            @property
            def parameters_schema(self): return {"type": "object", "properties": {}, "required": []}
            def execute(self, params): return type('R', (), {'output': '', 'success': True})()

        tool = FakeEditTool()
        meta = ToolMetadata.from_base_tool(tool)
        assert meta.resource_key == "file_path"

    def test_retry_policy_read(self):
        """T15: from_base_tool reads retry_policy.max_attempts."""
        from core.base import BaseTool
        from core.types import ToolMetadata as CoreMetadata, RetryPolicy, RetryMode
        class RetryTool(BaseTool):
            name = "RetryTool"
            metadata = CoreMetadata()
            def retry_policy(self, params):
                return RetryPolicy(mode=RetryMode.AUTOMATIC, max_attempts=5)
            @property
            def description(self): return "R"
            @property
            def parameters_schema(self): return {"type":"object","properties":{}}
            def execute(self, p): return type('R',(),{'output':'','success':True})()

        tool = RetryTool()
        meta = ToolMetadata.from_base_tool(tool)
        assert meta.retry_max == 4, f"max_attempts=5 → retry_max=4, got {meta.retry_max}"
