"""Phase C: Tool async — aexecute (CC tool.call async).

AC:
- ShellTool.aexecute executes via runtime.aexec (async subprocess)
- ShellTool.execute (sync compat) works in non-loop thread
- _RealTools.aexecute resolves + executes tools async
- file tools aexecute via to_thread (sync I/O wrapped)
"""

from __future__ import annotations

import asyncio

import pytest


def _make_shell_tool():
    from tools.shell_tool import ShellTool
    from core.process import LocalRuntime
    return ShellTool(runtime=LocalRuntime(workspace_root="."))


async def test_shell_aexecute_runs_command():
    """ShellTool.aexecute 通过 aexec 执行命令。"""
    tool = _make_shell_tool()
    result = await tool.aexecute({"command": "echo", "args": ["async-ok"]})
    assert result.success
    assert "async-ok" in result.output


async def test_shell_aexecute_blocked_command():
    """ShellTool.aexecute 拦截 blocked 命令（安全底线）。"""
    tool = _make_shell_tool()
    result = await tool.aexecute({"command": "rm", "args": ["-rf", "/"]})
    assert not result.success
    assert "blocked" in result.error.lower()


def test_shell_execute_sync_compat_in_thread():
    """ShellTool.execute (sync) 在无 loop 线程工作。"""
    tool = _make_shell_tool()
    result = tool.execute({"command": "echo", "args": ["sync-ok"]})
    assert result.success
    assert "sync-ok" in result.output


async def test_real_tools_aexecute_dynamic():
    """assemble() 的 _RealTools.aexecute 解析 + async 执行动态工具。"""
    import tempfile, os
    from composition.runtime_composition import assemble
    from runtime_core.ports import ToolSuccess

    comp = assemble(os.path.join(tempfile.mkdtemp(), "g.db"))
    tools = comp.runtime_ports.tools

    class _EchoTool:
        name = "echo_tool"
        async def aexecute(self, params):
            return ToolSuccess(tool_name="echo_tool", output=f"echo:{params.get('x')}")

    tools.register(_EchoTool())
    result = await tools.aexecute("echo_tool", {"x": "hello"}, "inv-1")
    assert isinstance(result, ToolSuccess)
    assert "echo:hello" in result.output


async def test_file_tool_aexecute():
    """FileReadTool.aexecute 通过 to_thread 读取文件。"""
    import tempfile, os
    from tools.file_tool import FileReadTool

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        with open(path, "w") as f:
            f.write("line1\nline2\n")

        tool = FileReadTool(workspace_root=tmp)
        result = await tool.aexecute({"path": "a.txt"})
        assert result.success
        assert "line1" in result.output
