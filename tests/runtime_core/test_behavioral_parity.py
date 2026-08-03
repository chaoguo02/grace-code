"""R3: CC 行为对齐测试 — 固化 tool_result 格式 + 错误重试契约。

对齐 Anthropic Tool Use / Claude Code Tools Reference：
- tool_result block 格式：{type: "tool_result", tool_use_id, content[, is_error]}
- 8 种 ToolErrorType 各自的 retry 行为与 ERROR_RETRY_MAP 一致
  (automatic → 重试 / approval → 修正后重试 / never → 不重试)

这些测试是契约回归护栏：任何偏离 CC 格式的改动都会在此 FAIL。
"""

from __future__ import annotations

import pytest

from runtime_core.ports import (
    ERROR_RETRY_MAP, ToolErrorType, ToolFailure, ToolSuccess, ToolDenied,
)


# ── tool_result CC 格式 ────────────────────────────────────────────────────

def test_tool_result_success_cc_format():
    """ToolSuccess → CC tool_result block（无 is_error）。"""
    block = ToolSuccess(
        tool_name="Read", output="file content", tool_use_id="call_1",
    ).to_chat_block()
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "call_1"
    assert block["content"] == "file content"
    assert "is_error" not in block or block["is_error"] is False


def test_tool_result_failure_cc_format():
    """ToolFailure → CC tool_result block + is_error: true。"""
    block = ToolFailure(
        tool_name="Bash", error="command not found",
        error_type=ToolErrorType.EXECUTION_ERROR,
    ).to_chat_block()
    assert block["type"] == "tool_result"
    assert block["is_error"] is True
    assert block["content"] == "command not found"


def test_tool_result_denied_cc_format():
    """ToolDenied → CC tool_result block + is_error + denied 提示。"""
    block = ToolDenied(tool_name="Write", reason="denied by rule").to_chat_block()
    assert block["type"] == "tool_result"
    assert block["is_error"] is True
    assert "denied" in block["content"].lower()


# ── ERROR_RETRY_MAP 全覆盖 ─────────────────────────────────────────────────

def test_error_retry_map_covers_all_8_types():
    """ERROR_RETRY_MAP 必须覆盖全部 8 种 ToolErrorType。"""
    all_types = set(ToolErrorType)
    assert set(ERROR_RETRY_MAP.keys()) == all_types, (
        f"ERROR_RETRY_MAP 缺类型: {all_types - set(ERROR_RETRY_MAP.keys())}"
    )


# ── retry 行为契约（对齐设计规范 2.2） ─────────────────────────────────────

@pytest.mark.parametrize("error_type", [
    ToolErrorType.TIMEOUT,
    ToolErrorType.NETWORK_ERROR,
    ToolErrorType.RESOURCE_EXHAUSTED,
])
def test_automatic_errors_are_retryable(error_type):
    """automatic 类错误（timeout/network/resource）→ 可自动重试。"""
    assert ERROR_RETRY_MAP[error_type] == "automatic"
    assert ToolFailure(tool_name="t", error="e", error_type=error_type).retryable


@pytest.mark.parametrize("error_type", [
    ToolErrorType.VALIDATION_ERROR,
    ToolErrorType.EXECUTION_ERROR,
])
def test_approval_errors_are_retryable(error_type):
    """approval 类错误（validation/execution）→ 修正后重试。"""
    assert ERROR_RETRY_MAP[error_type] == "approval"
    assert ToolFailure(tool_name="t", error="e", error_type=error_type).retryable


@pytest.mark.parametrize("error_type", [
    ToolErrorType.PERMISSION_DENIED,
    ToolErrorType.TOOL_NOT_FOUND,
    ToolErrorType.CANCELLED,
])
def test_never_errors_not_retryable(error_type):
    """never 类错误（permission/not_found/cancelled）→ 不重试。"""
    assert ERROR_RETRY_MAP[error_type] == "never"
    assert not ToolFailure(tool_name="t", error="e", error_type=error_type).retryable


def test_unknown_error_type_defaults_to_never():
    """未知 error_type 默认不重试（fail-closed）。"""
    # 用无效值构造应退化到不重试
    fail = ToolFailure(tool_name="t", error="e")
    # 默认 EXECUTION_ERROR → approval → retryable
    assert fail.retryable is True


# ── is_error 语义映射（T7） ────────────────────────────────────────────────

def test_failure_maps_to_is_error_true():
    """T7: ToolFailure → is_error=true（模型被告知失败并可重试/换方案）。"""
    assert ToolFailure(tool_name="t", error="boom").to_chat_block()["is_error"] is True


def test_success_does_not_set_is_error():
    """成功结果不设置 is_error（或为 false）。"""
    block = ToolSuccess(tool_name="t", output="ok", tool_use_id="c1").to_chat_block()
    assert block.get("is_error", False) is False
