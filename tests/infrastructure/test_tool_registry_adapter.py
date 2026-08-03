"""T18: ToolRegistryAdapter tests — register, resolve, list, metadata."""

from __future__ import annotations

import pytest
from infrastructure.tool_registry_adapter import ToolRegistryAdapter


class FakeTool:
    def __init__(self, name, read_only=False):
        self.name = name
        self._read_only = read_only
        self.metadata = type('M',(),{'path_parameter':''})()
    def isReadOnly(self, p=None): return self._read_only
    def concurrency_mode(self, p=None):
        from core.types import ToolConcurrency
        return ToolConcurrency.PARALLEL_SAFE if self._read_only else ToolConcurrency.SERIAL
    def execute(self, p): return type('R',(),{'output':'ok','success':True})()
    def retry_policy(self, p):
        from core.types import RetryPolicy
        return RetryPolicy()


class FakeRegistry:
    def __init__(self):
        self._tools = {}
    def register(self, tool):
        self._tools[tool.name] = tool
    def resolve_name(self, name):
        return name
    @property
    def tool_names(self):
        return list(self._tools.keys())


class TestToolRegistryAdapter:
    """T18: Adapter implements ToolRegistryPort."""

    def test_resolve_returns_tool(self):
        reg = FakeRegistry()
        tool = FakeTool("Read", read_only=True)
        reg.register(tool)
        adapter = ToolRegistryAdapter(registry=reg)
        assert adapter.resolve("Read") is not None

    def test_resolve_unknown_returns_none(self):
        adapter = ToolRegistryAdapter(registry=FakeRegistry())
        assert adapter.resolve("Unknown") is None

    def test_list_names(self):
        reg = FakeRegistry()
        reg.register(FakeTool("Read"))
        reg.register(FakeTool("Write"))
        adapter = ToolRegistryAdapter(registry=reg)
        names = adapter.list_names()
        assert "Read" in names
        assert "Write" in names

    def test_metadata_for(self):
        reg = FakeRegistry()
        reg.register(FakeTool("Read", read_only=True))
        adapter = ToolRegistryAdapter(registry=reg)
        meta = adapter.metadata_for("Read")
        assert meta is not None
        assert meta.read_only is True

    def test_thread_safe_register(self):
        import threading
        reg = FakeRegistry()
        adapter = ToolRegistryAdapter(registry=reg)
        def _reg():
            adapter.register(FakeTool("T"))
        threads = [threading.Thread(target=_reg) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert adapter.resolve("T") is not None
