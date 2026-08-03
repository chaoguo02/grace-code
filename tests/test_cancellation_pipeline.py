"""P0_3 Batch 2: CancellationHandle → Pipeline → ResourceGovernor — tests."""

from __future__ import annotations

import subprocess
import time

import pytest

from core.cancellation import CancellationHandle, ProcessRegistry, ProcessHandle


def _mk_req(request_id: str, session_id: str = "s1", **kw):
    from core.resource_governor import ResourceRequest, ResourceKind
    base = dict(
        request_id=request_id,
        root_session_id=session_id,
        session_id=session_id,
        resources={ResourceKind.TOOL_SLOT: 1},
    )
    base.update(kw)
    return ResourceRequest(**base)


class TestResourceGovernorCancel:

    def test_cancel_token_field(self):
        handle = CancellationHandle()
        req = _mk_req("r1", cancel_token=handle)
        assert req.cancel_token is handle

    def test_cancel_token_checked_in_admit_wait(self):
        """cancel_token with is_cancelled=True is checked inside admit_wait."""
        from core.resource_governor import ResourceGovernor
        from core.cancellation import CancellationHandle

        handle = CancellationHandle()
        req = _mk_req("test", cancel_token=handle)

        # Active token → not cancelled
        assert not ResourceGovernor._request_cancelled(req)

        # Cancelled token → cancelled
        handle.cancel("test")
        assert ResourceGovernor._request_cancelled(req)

    def test_active_token_grants_normally(self):
        from core.resource_governor import ResourceGovernor, AdmissionOutcome
        from config.schema import (
            ResourceGovernanceConfig, ResourceGovernanceWorkerConfig,
            ResourceGovernanceQueueConfig,
        )
        cfg = ResourceGovernanceConfig(
            mode="enforce",
            worker=ResourceGovernanceWorkerConfig(global_max=2, per_root_max=2),
            queue=ResourceGovernanceQueueConfig(max_size=10, timeout_seconds=30),
        )
        gov = ResourceGovernor(cfg)
        handle = CancellationHandle()
        result = gov.admit_wait(_mk_req("req", cancel_token=handle))
        assert result.outcome == AdmissionOutcome.GRANTED


class TestStreamingExecutorCancel:

    def test_stores_handle(self):
        from core.streaming_executor import StreamingToolExecutor
        from unittest.mock import MagicMock
        executor = StreamingToolExecutor(MagicMock())
        handle = CancellationHandle()
        executor.set_cancellation_handle(handle)
        assert executor._cancellation_handle is handle

    def test_stores_registry(self):
        from core.streaming_executor import StreamingToolExecutor
        from unittest.mock import MagicMock
        executor = StreamingToolExecutor(MagicMock())
        reg = ProcessRegistry()
        executor.set_process_registry(reg)
        assert executor._process_registry is reg


class TestProcessRegistryIsolation:

    def _spawn(self):
        return subprocess.Popen(
            ["python", "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def test_kill_run_isolation(self):
        reg = ProcessRegistry()
        p1, p2 = self._spawn(), self._spawn()
        reg.register(ProcessHandle(p1.pid, "s1", 1, "r1", "i1", p1))
        reg.register(ProcessHandle(p2.pid, "s1", 1, "r2", "i2", p2))
        assert reg.kill_run("s1", 1, "r1") >= 1
        assert _wait_dead(p1, 5)
        assert p2.poll() is None
        reg.kill_session("s1")

    def test_generation_isolation(self):
        reg = ProcessRegistry()
        p1, p2 = self._spawn(), self._spawn()
        reg.register(ProcessHandle(p1.pid, "s1", 1, "rx", "i1", p1))
        reg.register(ProcessHandle(p2.pid, "s1", 2, "rx", "i2", p2))
        assert reg.kill_run("s1", 1, "rx") >= 1
        assert _wait_dead(p1, 5)
        assert p2.poll() is None
        reg.kill_session("s1")


def _wait_dead(proc, timeout_s):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.1)
    return False
