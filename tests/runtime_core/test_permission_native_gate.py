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
    """bypassPermissions 下无 deny/ask 规则的工具放行（不因无交互回调误伤）。

    P0-2 语义：builtin 默认把 Write/Edit 标 ASK（bypass-immune），无交互回调
    时 ask 规则 fail-closed —— 所以这里改用不触发 ask 的工具（Read）验证
    "未匹配规则 → 放行"，与 CC headless 行为一致。
    """
    from tools.file_tool import FileReadTool

    def _lookup(name):
        ws = str(tmp_path)
        if name == "Write":
            return FileWriteTool(workspace_root=ws)
        if name == "Edit":
            return FileEditTool(workspace_root=ws)
        if name == "Read":
            return FileReadTool(workspace_root=ws)
        return None

    components = _assemble_with_lookup(tmp_path, _lookup, {"permission_mode": "bypassPermissions"})
    hook_input = PreToolUseInput(tool_name="Read", tool_input={}, tool_use_id="t1")
    result = components.runtime_ports.hooks.check("PreToolUse", hook_input, tool_name="Read")
    assert result.allowed is True, (
        f"无 deny/ask 规则 + bypass 应放行，got allowed={result.allowed} "
        f"reason={result.reason!r}"
    )


def test_ask_rule_fails_closed_without_callback(tmp_path):
    """P0-2: 显式 ask 规则 + 无交互回调 → fail-closed（CC headless ask=auto-deny）。

    用户显式在 settings.json 配 "Write": "ask" → native headless 无回调必须拒绝。
    注意：native builtin 不再把 Write/Edit 标 ASK（acceptEdits 语义）——只有
    显式 ask 规则才会走到 fail-closed。
    """
    result = _native_check(
        tmp_path,
        {
            "permission_rules": {"Write": "ask"},
            "permission_mode": "bypassPermissions",
        },
        "Write",
    )
    assert result.allowed is False, (
        f"显式 ask 规则无回调应 fail-closed，got allowed={result.allowed} "
        f"reason={result.reason!r}"
    )


def _assemble_with_lookup(tmp_path, lookup, hook_settings):
    from composition.runtime_composition import assemble
    return assemble(
        str(tmp_path / "native.db"),
        hook_settings=hook_settings,
        tool_registry=lookup,
    )


# ── P0-3/P0-4: per-session interactive ask + Layer 1 + isolation ────────────


def _assemble_project_root(tmp_path, lookup, hook_settings):
    """Assemble with db under <repo>/.grace/ so project_root resolves to tmp."""
    from composition.runtime_composition import assemble
    grace_dir = tmp_path / ".grace"
    grace_dir.mkdir(exist_ok=True)
    return assemble(
        str(grace_dir / "native.db"),
        hook_settings=hook_settings,
        tool_registry=lookup,
    )


def _allow_once_callback(request):
    """Simulate a user clicking 'Allow once' in the web approval card."""
    from hitl.pipeline import PromptDecision, PromptAction
    return PromptDecision(action=PromptAction.ALLOW_ONCE)


def _deny_callback(request):
    from hitl.pipeline import PromptDecision, PromptAction
    return PromptDecision(action=PromptAction.DENY)


def _write_lookup(tmp_path):
    def _lookup(name):
        ws = str(tmp_path)
        if name == "Write":
            return FileWriteTool(workspace_root=ws)
        if name == "Edit":
            return FileEditTool(workspace_root=ws)
        return None
    return _lookup


def test_layer1_protected_path_blocked(tmp_path):
    """P0-4: Layer 1 validateInput — .git 保护路径 bypass-immune。

    即使 bypassPermissions，写入 .git 也被硬拒绝（安全底线）。
    """
    comp = _assemble_project_root(
        tmp_path, _write_lookup(tmp_path),
        {"permission_mode": "bypassPermissions"},
    )
    target = str(tmp_path / ".git" / "config")
    hi = PreToolUseInput(
        tool_name="Write", tool_input={"path": target, "content": "x"},
        tool_use_id="t1",
    )
    result = comp.runtime_ports.hooks.check("PreToolUse", hi, tool_name="Write")
    assert result.allowed is False
    assert "protected" in (result.reason or "").lower(), result.reason


def test_ask_with_callback_is_interactive(tmp_path):
    """P0-3: ask 规则 + per-session callback → 用户批准 → 放行。"""
    comp = _assemble_project_root(
        tmp_path, _write_lookup(tmp_path),
        {"permission_rules": {"Write": "ask"}},
    )
    hooks = comp.runtime_ports.hooks
    hooks.register_session_confirm("sess-1", _allow_once_callback)
    hi = PreToolUseInput(
        tool_name="Write",
        tool_input={"path": str(tmp_path / "a.txt"), "content": "x"},
        tool_use_id="t1", session_id="sess-1",
    )
    result = hooks.check("PreToolUse", hi, tool_name="Write")
    assert result.allowed is True, result.reason


def test_ask_with_callback_deny_blocks(tmp_path):
    """P0-3: ask 规则 + callback 拒绝 → 拦截。"""
    comp = _assemble_project_root(
        tmp_path, _write_lookup(tmp_path),
        {"permission_rules": {"Write": "ask"}},
    )
    hooks = comp.runtime_ports.hooks
    hooks.register_session_confirm("sess-1", _deny_callback)
    hi = PreToolUseInput(
        tool_name="Write",
        tool_input={"path": str(tmp_path / "a.txt"), "content": "x"},
        tool_use_id="t1", session_id="sess-1",
    )
    result = hooks.check("PreToolUse", hi, tool_name="Write")
    assert result.allowed is False


def test_per_session_callback_isolation(tmp_path):
    """P0-3: 两个 session 各自回调不串。

    sess-1 批准、sess-2 拒绝 —— 各自按自己的 callback 决策。
    """
    comp = _assemble_project_root(
        tmp_path, _write_lookup(tmp_path),
        {"permission_rules": {"Write": "ask"}},
    )
    hooks = comp.runtime_ports.hooks
    hooks.register_session_confirm("sess-1", _allow_once_callback)
    hooks.register_session_confirm("sess-2", _deny_callback)

    hi1 = PreToolUseInput(
        tool_name="Write",
        tool_input={"path": str(tmp_path / "a.txt"), "content": "x"},
        tool_use_id="t1", session_id="sess-1",
    )
    r1 = hooks.check("PreToolUse", hi1, tool_name="Write")
    assert r1.allowed is True, r1.reason

    hi2 = PreToolUseInput(
        tool_name="Write",
        tool_input={"path": str(tmp_path / "b.txt"), "content": "x"},
        tool_use_id="t2", session_id="sess-2",
    )
    r2 = hooks.check("PreToolUse", hi2, tool_name="Write")
    assert r2.allowed is False
