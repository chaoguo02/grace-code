"""R1: PermissionPipeline 接入 Native StepLoop — Before/Target 测试。

对齐 CC "权限是动态流水线"：permission_rules（settings.json）必须
在 Native 路径生效。PreToolUse 时先经 PermissionPipeline 评估
（deny/ask/allow + 权限模式 + 信任累积），再执行 hook dispatcher。

Before（实现前）：本文件测试 FAIL —— 证明 permission_rules 配置后
Native 路径绕过规则（_RealHooks 只走 hook dispatcher）。
Target（实现后）：全部 PASS。
"""

from __future__ import annotations

import pytest

from hook_core.inputs import PreToolUseInput
from tools.file_edit_tool import FileEditTool
from tools.file_tool import FileWriteTool


def _lookup_factory(tmp_path):
    """返回可 resolve Write/Edit 的 callable registry。"""
    def _lookup(name):
        ws = str(tmp_path)
        if name == "Write":
            return FileWriteTool(workspace_root=ws)
        if name == "Edit":
            return FileEditTool(workspace_root=ws)
        return None
    return _lookup


def _native_check(tmp_path, hook_settings, tool_name="Write"):
    from composition.runtime_composition import assemble
    components = assemble(
        str(tmp_path / "native.db"),
        hook_settings=hook_settings,
        tool_registry=_lookup_factory(tmp_path),
    )
    hook_input = PreToolUseInput(tool_name=tool_name, tool_input={}, tool_use_id="t1")
    return components.runtime_ports.hooks.check(
        "PreToolUse", hook_input, tool_name=tool_name,
    )


def test_permission_rules_block_in_native(tmp_path):
    """permission_rules: deny Write → Native PreToolUse 拦截。"""
    result = _native_check(
        tmp_path, {"permission_rules": {"Write": "deny"}}, "Write",
    )
    assert result.allowed is False, (
        f"deny 规则应在 Native 拦截 Write，got allowed={result.allowed} "
        f"reason={result.reason!r}"
    )


def test_permission_allow_rule_passes_in_native(tmp_path):
    """permission_rules: allow Read → Native 放行。"""
    result = _native_check(
        tmp_path, {"permission_rules": {"Read": "allow"}}, "Write",
    )
    # Write 无匹配规则 → 不因 permission 拦截（走 dispatcher）
    assert result.allowed is True, (
        f"无匹配 deny 规则应放行，got allowed={result.allowed}"
    )


def test_permission_rule_matches_edit(tmp_path):
    """deny Edit → Native 拦截 Edit。"""
    result = _native_check(
        tmp_path, {"permission_rules": {"Edit": "deny"}}, "Edit",
    )
    assert result.allowed is False


def test_bypass_permissions_does_not_bypass_deny(tmp_path):
    """bypassPermissions 不绕过 deny 规则（CC 事实：deny 是绝对安全底线）。

    T19 语义：bypassPermissions 跳过"需要确认"的提示，但 deny 规则是
    bypass-immune 的硬拒绝 —— Native 路径同样遵守。
    """
    result = _native_check(
        tmp_path,
        {
            "permission_rules": {"Write": "deny"},
            "permission_mode": "bypassPermissions",
        },
        "Write",
    )
    assert result.allowed is False, (
        f"deny 规则在 bypassPermissions 下仍须拦截，got allowed={result.allowed}"
    )


def test_bypass_permissions_allows_unmatched_tool(tmp_path):
    """bypassPermissions 下无 deny 规则的工具放行（不因无交互回调误伤）。"""
    result = _native_check(
        tmp_path,
        {"permission_mode": "bypassPermissions"},
        "Write",
    )
    assert result.allowed is True, (
        f"无 deny 规则 + bypass 应放行，got allowed={result.allowed}"
    )
