from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from runtime.mcp.client import MCPCallResult
from runtime.mcp.sync_bridge import (
    ExecutionPolicy,
    MCPToolExhaustedError,
    MCPToolTimeoutError,
    SyncMCPToolManager,
)


class TestExecutionPolicy:
    def test_default_policy(self):
        policy = ExecutionPolicy()

        assert policy.timeout == 30.0
        assert policy.max_retries == 2

    def test_backoff_calculation_with_jitter_range(self):
        policy = ExecutionPolicy(backoff_base=1.0, backoff_factor=2.0, backoff_max=8.0)

        assert 0.9 <= policy.get_backoff(0) <= 1.1
        assert 1.8 <= policy.get_backoff(1) <= 2.2
        assert 7.2 <= policy.get_backoff(3) <= 8.8


class TestExecuteToolRetry:
    def setup_method(self):
        self.manager = SyncMCPToolManager.__new__(SyncMCPToolManager)
        self.manager._closed = False
        self.manager._default_policy = ExecutionPolicy(timeout=1.0, max_retries=2, backoff_base=0.001)
        self.manager._tool_map = {"mcp__srv__tool": ("srv", "tool")}
        self.manager._bridges = {"srv": MagicMock(is_connected=True)}

    def test_success_on_first_attempt(self):
        expected = MCPCallResult(content=[{"text": "ok"}])
        self.manager._execute_once = MagicMock(return_value=expected)

        result = self.manager.execute_tool("mcp__srv__tool", {})

        assert result is expected
        assert self.manager._execute_once.call_count == 1

    def test_retry_on_timeout(self):
        expected = MCPCallResult(content=[{"text": "ok"}])
        self.manager._execute_once = MagicMock(side_effect=[TimeoutError("boom"), expected])

        result = self.manager.execute_tool("mcp__srv__tool", {})

        assert result is expected
        assert self.manager._execute_once.call_count == 2

    def test_exhausted_retries(self):
        self.manager._execute_once = MagicMock(side_effect=TimeoutError("boom"))

        with pytest.raises(MCPToolExhaustedError) as exc_info:
            self.manager.execute_tool("mcp__srv__tool", {})

        assert exc_info.value.attempts == 3

    def test_non_idempotent_no_retry(self):
        self.manager._execute_once = MagicMock(side_effect=TimeoutError("boom"))

        with pytest.raises(MCPToolExhaustedError) as exc_info:
            self.manager.execute_tool("mcp__srv__tool", {}, idempotent=False)

        assert exc_info.value.attempts == 1
        assert self.manager._execute_once.call_count == 1

    def test_non_retryable_exception_raises_immediately(self):
        self.manager._execute_once = MagicMock(side_effect=ValueError("bad input"))

        with pytest.raises(ValueError, match="bad input"):
            self.manager.execute_tool("mcp__srv__tool", {})

        assert self.manager._execute_once.call_count == 1

    def test_call_tool_converts_exhaustion_to_error_result(self):
        self.manager._execute_once = MagicMock(side_effect=TimeoutError("boom"))

        result = self.manager.call_tool("mcp__srv__tool", {})

        assert result.is_error is True
        assert "failed after 3 attempt" in result.text
        assert result.metadata is not None
        assert result.metadata["mcp_is_error"] is True

    def test_execute_once_rejects_disconnected_server(self):
        self.manager._bridges = {"srv": MagicMock(is_connected=False)}

        with pytest.raises(ConnectionError, match="not connected"):
            self.manager._execute_once("mcp__srv__tool", {}, timeout=1.0)

    def test_execute_once_rejects_invalid_name(self):
        with pytest.raises(KeyError, match="Invalid MCP tool name"):
            self.manager._execute_once("not_mcp", {}, timeout=1.0)

    def test_execute_once_sync_timeout_cancels_future(self):
        future = MagicMock()
        future.result.side_effect = TimeoutError()
        self.manager._loop = MagicMock()

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                "runtime.mcp.sync_bridge.asyncio.run_coroutine_threadsafe",
                lambda _coro, _loop: future,
            )
            with pytest.raises(MCPToolTimeoutError):
                self.manager._execute_once("mcp__srv__tool", {}, timeout=0.01)

        future.cancel.assert_called_once()
