"""runtime_core/message_validator.py

协议合规预检层 — 在 API 调用前捕获语义违规，把 400 变成本地断言。

CC 对齐：CC 在发送前有一个轻量级的 validate_messages() 纯函数，
专门做 Anthropic API 语义预检。它不修改数据，只抛异常或告警。

约束来源：Anthropic Messages API 文档的语义校验规则。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from runtime_core.native_message import (
    NativeConversation,
    NativeMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)


# ── Validation result ────────────────────────────────────────────────────────


@dataclass
class ValidationResult:
    """预检结果 — 通过或有具体错误列表。"""
    valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        self.valid = False
        self.errors.append(msg)

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def raise_if_invalid(self) -> None:
        """如果无效则抛出 ValueError（含所有错误）。"""
        if not self.valid:
            raise ValueError(
                "Message validation failed:\n  - "
                + "\n  - ".join(self.errors)
            )

    def merge(self, other: ValidationResult) -> None:
        self.valid = self.valid and other.valid
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)


# ── Main entry point ─────────────────────────────────────────────────────────


def validate_messages(conversation: NativeConversation) -> ValidationResult:
    """预检整个对话的消息序列。

    在 NativeBackend.invoke() 入口处调用，把 API 400 变成本地断言失败。
    纯函数 — 不修改数据，只返回校验结果。
    """
    result = ValidationResult()
    messages = conversation.messages

    if not messages:
        result.add_error("Cannot send empty conversation to API")
        return result

    # ── Rule 1: System messages must be first ───────────────────────────
    _validate_system_position(messages, result)

    # ── Rule 2: No consecutive user roles ───────────────────────────────
    _validate_alternating_roles(messages, result)

    # ── Rule 3: Each message has valid content blocks ───────────────────
    for i, msg in enumerate(messages):
        _validate_message_blocks(msg, i, result)

    # ── Rule 4: tool_use → tool_result pairing (best-effort pre-check) ──
    _validate_tool_pairing(messages, result)

    # ── Rule 5: Content size sanity ─────────────────────────────────────
    _validate_content_sizes(messages, result)

    return result


# ── Individual rules ─────────────────────────────────────────────────────────


def _validate_system_position(
    messages: tuple[NativeMessage, ...],
    result: ValidationResult,
) -> None:
    """system 消息必须位于序列开头，且不能包含 tool_use。"""
    seen_non_system = False
    for i, msg in enumerate(messages):
        if msg.role == "system":
            if seen_non_system:
                result.add_error(
                    f"Message[{i}]: system message must be at the beginning, "
                    f"but non-system messages already appeared"
                )
            if msg.has_tool_uses:
                result.add_error(
                    f"Message[{i}]: system message must not contain tool_use blocks"
                )
        else:
            seen_non_system = True


def _validate_alternating_roles(
    messages: tuple[NativeMessage, ...],
    result: ValidationResult,
) -> None:
    """Anthropic API 要求 user 和 assistant 交替出现。

    连续的 user 消息（例如 user + user tool_result）是合法的 — Anthropic
    允许 tool_result 跟在 user 消息后面，因为 tool_result 以 user role 发送。
    但连续的两个纯 user 文本消息不合法。
    """
    prev_role = ""
    for i, msg in enumerate(messages):
        if msg.role == "system":
            continue

        role = msg.role
        # tool_result 消息（user role 但只有 ToolResultBlock）不算 user
        effective_role = _effective_role(msg)

        if effective_role == prev_role and effective_role == "user":
            result.add_warning(
                f"Message[{i}]: consecutive user messages without tool_result — "
                f"Anthropic API may reject this. Consider merging user messages."
            )

        # assistant 不能连续出现
        if effective_role == prev_role and effective_role == "assistant":
            result.add_error(
                f"Message[{i}]: consecutive assistant messages. "
                f"Anthropic requires alternating user/assistant roles."
            )

        prev_role = effective_role


def _validate_message_blocks(
    msg: NativeMessage,
    index: int,
    result: ValidationResult,
) -> None:
    """验证单个消息的 content blocks 结构。"""
    if not msg.content:
        result.add_error(
            f"Message[{index}]: content must not be empty (role={msg.role})"
        )
        return

    for block in msg.content:
        if isinstance(block, ToolResultBlock):
            # tool_result.content 不能为空数组
            if isinstance(block.content, tuple) and len(block.content) == 0:
                result.add_error(
                    f"Message[{index}]: tool_result block "
                    f"(tool_use_id={block.tool_use_id}) has empty content array. "
                    f"Anthropic API requires at least one content block."
                )
            # tool_result.content 为纯文本时不能是空字符串
            if isinstance(block.content, str) and not block.content.strip():
                result.add_warning(
                    f"Message[{index}]: tool_result block "
                    f"(tool_use_id={block.tool_use_id}) has empty content string."
                )

        elif isinstance(block, ToolUseBlock):
            # tool_use 必须在 assistant role 中
            if msg.role != "assistant":
                result.add_error(
                    f"Message[{index}]: tool_use block (id={block.id}, name={block.name}) "
                    f"found in role={msg.role}. Tool_use must be in assistant messages."
                )
            # tool_use 必须有非空 id
            if not block.id:
                result.add_error(
                    f"Message[{index}]: tool_use block (name={block.name}) "
                    f"has empty id. Anthropic API requires unique tool_use ids."
                )
            # tool_use 必须有非空 name
            if not block.name:
                result.add_error(
                    f"Message[{index}]: tool_use block (id={block.id}) "
                    f"has empty name."
                )

        elif isinstance(block, TextBlock):
            # text block 的 text 可以为空（thought 场景），但给出警告
            if not block.text and msg.role == "assistant":
                # assistant 的空 text + tool_use 是合法的
                pass


def _validate_tool_pairing(
    messages: tuple[NativeMessage, ...],
    result: ValidationResult,
) -> None:
    """验证 tool_use → tool_result 配对（best-effort 预检）。

    遍历所有 tool_use，检查后面是否有对应的 tool_result。
    这不是严格校验（对话可能还在进行中），但可以提前发现明显错误。
    """
    tool_use_ids: set[str] = set()
    matched_ids: set[str] = set()

    for msg in messages:
        for block in msg.content:
            if isinstance(block, ToolUseBlock) and block.id:
                tool_use_ids.add(block.id)
            elif isinstance(block, ToolResultBlock) and block.tool_use_id:
                matched_ids.add(block.tool_use_id)

    unmatched = tool_use_ids - matched_ids
    if unmatched:
        result.add_warning(
            f"Unmatched tool_use ids (no tool_result found in conversation): "
            f"{sorted(unmatched)}. This is acceptable if conversation is in-progress, "
            f"but will cause API errors if submitted."
        )


def _validate_content_sizes(
    messages: tuple[NativeMessage, ...],
    result: ValidationResult,
) -> None:
    """验证内容大小在合理范围内。"""
    total_text_chars = 0
    for msg in messages:
        for block in msg.content:
            if isinstance(block, TextBlock):
                total_text_chars += len(block.text)
            elif isinstance(block, ToolResultBlock):
                if isinstance(block.content, str):
                    total_text_chars += len(block.content)

    # 单个消息的 content 文本不能过大（Anthropic 有隐式限制）
    if total_text_chars > 1_000_000:
        result.add_warning(
            f"Total text content is {total_text_chars} chars (>1M). "
            f"Consider compressing tool outputs."
        )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _effective_role(msg: NativeMessage) -> str:
    """获取消息的"有效 role"。

    tool_result 消息以 user role 发送，但语义上是 tool 响应。
    连续两个 user 消息（其中一个是 tool_result）在 Anthropic API 中合法。
    """
    if msg.has_tool_results and not msg.has_tool_uses:
        return "tool_result"
    if msg.has_tool_uses:
        return "assistant"
    return msg.role
