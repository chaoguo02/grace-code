"""R2: Native 动态工具注册 — Before/Target 测试。

对齐 CC T21-T23：工具可运行期注册/注销（MCP 动态接入）、mcp__server__tool
前缀别名解析。_RealTools 实现 ToolRegistryPort 协议（register/unregister/
resolve/list_names/metadata_for）。

Before（实现前）：本文件 FAIL —— _RealTools 无 register 等动态接口。
Target（实现后）：全部 PASS。
"""

from __future__ import annotations

import os
import tempfile

import pytest

from core.base import BaseTool, ToolMetadata, ToolResult


class _DummyTool(BaseTool):
    name = "Dummy"
    description = "test tool"
    parameters_schema = {"type": "object", "properties": {}}
    metadata = ToolMetadata()

    def execute(self, params):
        return ToolResult(success=True, output="dummy-ok")


def _native_tools():
    d = tempfile.mkdtemp()
    from composition.runtime_composition import assemble
    components = assemble(os.path.join(d, "db.db"))
    return components.runtime_ports.tools


def test_dynamic_register_method_exists():
    """_RealTools 实现 register（ToolRegistryPort 协议）。"""
    tools = _native_tools()
    assert hasattr(tools, "register"), "_RealTools 应实现 register()"
    assert hasattr(tools, "unregister"), "_RealTools 应实现 unregister()"


def test_dynamic_register_resolves():
    tools = _native_tools()
    tool = _DummyTool()
    tools.register(tool)
    resolved = tools.resolve("Dummy")
    assert resolved is tool, "register 后 resolve 应命中同一实例"


def test_dynamic_unregister_removes():
    tools = _native_tools()
    tool = _DummyTool()
    tools.register(tool)
    assert tools.resolve("Dummy") is tool
    tools.unregister("Dummy")
    assert tools.resolve("Dummy") is None, "unregister 后 resolve 不应命中"


def test_dynamic_register_executes():
    """register 的工具可被 execute 调用（非 fake）。"""
    tools = _native_tools()
    tools.register(_DummyTool())
    from runtime_core.ports import ToolSuccess
    result = tools.execute("Dummy", {})
    assert isinstance(result, ToolSuccess), f"动态工具执行应返回 ToolSuccess，got {result}"
    assert result.output == "dummy-ok"


def test_mcp_prefix_alias_resolves():
    """mcp__server__tool 前缀可解析到注册的 MCP 工具。"""
    tools = _native_tools()
    mcp_tool = _DummyTool()
    mcp_tool.name = "mcp__server__dummy"
    tools.register(mcp_tool)
    assert tools.resolve("mcp__server__dummy") is mcp_tool


def test_dynamic_list_names():
    tools = _native_tools()
    tools.register(_DummyTool())
    assert "Dummy" in tools.list_names()
