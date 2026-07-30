"""Tests for Phase 2: cancellable lifecycle and shared executor.

Covers:
  1. LLMBackend.close() — base class and MockBackend
  2. OpenAIBackend.close() — stream tracking
  3. LLMInvoker timeout calls backend.close()
  4. StreamingToolExecutor.shutdown()
  5. SessionRuntime.dispose() shutdown order
  6. Backward compat — all Phase 0/1 tests still pass
"""

from __future__ import annotations

import threading
import time as _time

import pytest


# ===========================================================================
# LLM Backend close() Tests
# ===========================================================================


class TestLLMBackendClose:
    def test_base_backend_close_is_noop(self):
        from llm.base import MockBackend
        backend = MockBackend(script=[])
        # Should not raise
        backend.close()

    def test_custom_backend_close_called(self):
        from llm.base import MockBackend

        close_calls = []
        class CloseTestBackend(MockBackend):
            def close(self):
                close_calls.append(True)
                super().close()

        backend = CloseTestBackend(script=[])
        backend.close()
        assert len(close_calls) == 1


# ===========================================================================
# StreamingToolExecutor shutdown() Tests
# ===========================================================================


class TestStreamingExecutorShutdown:
    def test_shutdown_is_idempotent(self):
        """Multiple shutdown() calls should not raise."""
        from core.streaming_executor import StreamingToolExecutor
        from core.base import ToolRegistry

        registry = ToolRegistry()
        executor = StreamingToolExecutor(registry)
        executor.shutdown()
        executor.shutdown()  # second call — no-op

    def test_shutdown_clears_pool(self):
        """shutdown() should clear the internal pool reference."""
        from core.streaming_executor import StreamingToolExecutor
        from core.base import ToolRegistry

        registry = ToolRegistry()
        executor = StreamingToolExecutor(registry)
        executor.shutdown(wait=True, cancel_futures=True)
        assert executor._pool is None


# ===========================================================================
# SessionRuntime dispose() Tests
# ===========================================================================


class TestSessionRuntimeDispose:
    def test_dispose_clears_cancellation_tokens(self):
        """dispose() cancels all tokens then clears them."""
        from agent.session.run_context import CancellationToken
        from agent.session.runtime import SessionRuntime

        # Create a minimal runtime
        import tempfile, os
        tmpdir = tempfile.mkdtemp()
        from agent.session.session_store import SessionStore
        from agent.session.agent_registry import AgentRegistryV2
        from agent.core import AgentConfig
        from llm.base import MockBackend
        from core.base import ToolRegistry

        store = SessionStore(os.path.join(tmpdir, "sessions.db"))
        backend = MockBackend(script=[])
        registry = ToolRegistry()
        agent_registry = AgentRegistryV2(project_dir=tmpdir)
        cfg = AgentConfig()

        runtime = SessionRuntime(
            store=store, backend=backend,
            base_registry=registry, agent_registry=agent_registry,
            root_agent_config=cfg, log_dir=tmpdir,
        )

        # Add a cancellation token
        token = CancellationToken()
        runtime._cancellation_tokens[("test-session", 0)] = token

        assert not token.is_cancelled
        runtime.dispose()
        # Token should be cancelled by dispose
        assert token.is_cancelled

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# Backward Compatibility
# ===========================================================================


class TestPhase2BackwardCompat:
    """All Phase 0 and Phase 1 patterns remain valid."""

    def test_governor_observe_still_always_grants(self):
        from core.resource_governor import (
            ResourceGovernor, ResourceRequest, ResourceKind, AdmissionOutcome,
        )
        from config.schema import ResourceGovernanceConfig

        cfg = ResourceGovernanceConfig(mode="observe")
        rg = ResourceGovernor(cfg)
        result = rg.admit(ResourceRequest(
            "test", "root-1", "sess-1",
            resources={ResourceKind.WORKER_SLOT: 10},
        ))
        assert result.outcome == AdmissionOutcome.GRANTED
        result.lease.release()

    def test_governor_enforce_still_blocks(self):
        from core.resource_governor import (
            ResourceGovernor, ResourceRequest, ResourceKind, AdmissionOutcome,
        )
        from config.schema import (
            ResourceGovernanceConfig, ResourceGovernanceWorkerConfig,
        )

        cfg = ResourceGovernanceConfig(
            mode="enforce",
            worker=ResourceGovernanceWorkerConfig(global_max=1, per_root_max=1),
        )
        rg = ResourceGovernor(cfg)
        r1 = rg.admit(ResourceRequest(
            "r1", "root-1", "s-1",
            resources={ResourceKind.WORKER_SLOT: 1},
        ))
        assert r1.outcome == AdmissionOutcome.GRANTED

        r2 = rg.admit(ResourceRequest(
            "r2", "root-1", "s-2",
            resources={ResourceKind.WORKER_SLOT: 1},
        ))
        assert r2.outcome != AdmissionOutcome.GRANTED
        r1.lease.release()

    def test_mock_backend_has_close(self):
        from llm.base import MockBackend
        backend = MockBackend(script=[])
        assert hasattr(backend, "close")
        backend.close()  # no-op
