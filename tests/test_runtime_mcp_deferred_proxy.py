from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from runtime.mcp.tool_adapter import deferred_mcp_tool
from runtime.tool import ToolUseContext


def _make_tool(**kwargs):
    defaults = {
        "name": "mcp__srv__tool",
        "description": "test",
        "input_schema": {"type": "object", "properties": {}},
        "execute_fn": MagicMock(return_value="result"),
        "server_name": "srv",
        "original_tool_name": "tool",
    }
    defaults.update(kwargs)
    return deferred_mcp_tool(**defaults)


class TestDeferredMCPTool:
    def test_not_connected_initially(self):
        tool = _make_tool(connect_fn=MagicMock())

        assert tool.mcp_props.is_deferred is True
        assert tool.is_connected() is False

    def test_first_call_triggers_connect(self):
        connect_fn = MagicMock()
        execute_fn = MagicMock(return_value="result")
        tool = _make_tool(connect_fn=connect_fn, execute_fn=execute_fn)

        result = tool.execute({"key": "value"})

        connect_fn.assert_called_once()
        execute_fn.assert_called_once_with({"key": "value"})
        assert result == "result"
        assert tool.is_connected() is True

    def test_second_call_skips_connect(self):
        connect_fn = MagicMock()
        execute_fn = MagicMock(return_value="ok")
        tool = _make_tool(connect_fn=connect_fn, execute_fn=execute_fn)

        tool.execute({})
        tool.execute({})

        assert connect_fn.call_count == 1
        assert execute_fn.call_count == 2

    def test_connect_failure_cached(self):
        connect_fn = MagicMock(side_effect=ConnectionError("refused"))
        tool = _make_tool(connect_fn=connect_fn)

        with pytest.raises(RuntimeError, match="refused"):
            tool.execute({})

        assert tool.is_connected() is False
        assert isinstance(tool.connect_error(), ConnectionError)

    def test_thread_safety_connects_once(self):
        connect_fn = MagicMock()
        execute_fn = MagicMock(return_value="ok")
        tool = _make_tool(connect_fn=connect_fn, execute_fn=execute_fn)
        errors = []
        barrier = threading.Barrier(10)

        def worker():
            try:
                barrier.wait(timeout=5)
                tool.execute({})
            except Exception as exc:  # pragma: no cover - failure details asserted below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert errors == []
        assert connect_fn.call_count == 1
        assert execute_fn.call_count == 10

    def test_to_api_schema_without_connection(self):
        tool = _make_tool(connect_fn=MagicMock())

        schema = tool.to_api_schema()

        assert schema["function"]["name"] == "mcp__srv__tool"
        assert schema["_meta"]["is_deferred"] is True
        assert schema["_meta"]["is_connected"] is False
        assert schema["_meta"]["server_name"] == "srv"

    def test_async_call_converts_execute_result(self):
        async def scenario():
            tool = _make_tool(execute_fn=MagicMock(return_value="async result"))
            result = await tool.call({}, ToolUseContext())
            assert result.output == "async result"
            assert result.metadata == {}

        import asyncio
        asyncio.run(scenario())
