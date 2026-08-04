"""runtime_core/conversation_state.py

会话状态管理器 — 保证 Anthropic 协议完整性。

五条架构原则第 2 条：协议完整性应由"会话状态管理器"保证，而非循环控制器。
StepLoop 永远不需要知道 tool_use_id 长什么样，它只知道"我执行了一个工具，结果是 X"。

CC 对齐：
- 每个 tool_use block 必须有对应的 tool_result block（is_error=True 兜底）
- assistant 消息中的 tool_use 按模型原始顺序保留
- tool_result 以 user role 发送（Anthropic API 要求）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from runtime_core.model_actions import (
    AssistantText,
    ModelAction,
    ModelFailure,
    ModelRefusal,
    ModelStop,
    ToolCall,
    ToolCallBatch,
)
from runtime_core.native_message import (
    NativeConversation,
    NativeMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from runtime_core.ports import ToolDenied, ToolFailure, ToolOutcome, ToolSuccess


def _params_to_dict(params) -> dict:
    """Normalize FrozenJsonObject / dict params to plain dict."""
    if params is None:
        return {}
    if isinstance(params, dict):
        return dict(params)
    items = getattr(params, "items", None)
    if isinstance(items, tuple):
        return dict(items)
    if callable(items):
        return dict(items())
    if hasattr(params, "__dataclass_fields__"):
        return dict(params.items())
    return {}


# ── ConversationState ────────────────────────────────────────────────────────


@dataclass
class ConversationState:
    """会话状态管理器 — 保证 Anthropic 协议完整性。

    StepLoop 只需调用：
    - add_user_message(text)
    - add_assistant_message(model_action)
    - add_tool_result(outcome, tool_call)

    它永远不需要知道 tool_use_id 长什么样。
    ConversationState 内部处理 ID 匹配、block 构造、错误兜底。
    """

    _messages: list[NativeMessage] = field(default_factory=list)
    _pending_tool_uses: dict[str, ToolUseBlock] = field(default_factory=dict)
    """跟踪所有已发出但未收到 tool_result 的 tool_use。
    用于：错误兜底（未匹配的 tool_use 生成 is_error 占位）。
    """

    # ── Message addition API ─────────────────────────────────────────────

    def add_user_message(self, text: str) -> None:
        """追加 user 消息。"""
        self._messages.append(NativeMessage.user(text))

    def add_system_message(self, text: str) -> None:
        """追加 system 消息。"""
        self._messages.append(NativeMessage.system(text))

    def add_assistant_message(self, action: ModelAction) -> None:
        """从 ModelAction 自动生成正确的 assistant NativeMessage。

        - AssistantText → [TextBlock]
        - ModelStop → [TextBlock]
        - ToolCall → [TextBlock(thought), ToolUseBlock(id,name,input)]
        - ToolCallBatch → [TextBlock(thought), ToolUseBlock...]
        - ModelRefusal → [TextBlock(refusal)]
        - ModelFailure → [TextBlock(error)] (retryable 则跳过，不追加)
        """
        if isinstance(action, AssistantText):
            msg = NativeMessage.assistant_text(action.text)
            self._messages.append(msg)

        elif isinstance(action, ModelStop):
            text = action.text or action.stop_reason or ""
            msg = NativeMessage.assistant_text(text)
            self._messages.append(msg)

        elif isinstance(action, (ToolCall, ToolCallBatch)):
            calls = (action,) if isinstance(action, ToolCall) else action.calls
            thought = self._extract_thought(action)

            tool_uses: list[ToolUseBlock] = []
            for tc in calls:
                tu = ToolUseBlock(
                    id=tc.id,
                    name=tc.name,
                    input=_params_to_dict(tc.params),
                )
                tool_uses.append(tu)
                self._pending_tool_uses[tc.id] = tu

            msg = NativeMessage.assistant_with_tools(thought, tuple(tool_uses))
            self._messages.append(msg)

        elif isinstance(action, ModelRefusal):
            msg = NativeMessage.assistant_text(
                action.reason or "Model refused to respond"
            )
            self._messages.append(msg)

        elif isinstance(action, ModelFailure):
            if action.retryable:
                # 不追加消息 — 等待重试
                return
            msg = NativeMessage.assistant_text(
                f"Model error: {action.error}"
            )
            self._messages.append(msg)

    def add_tool_result(
        self,
        outcome: ToolOutcome,
        tool_call: ToolCall,
    ) -> None:
        """自动构造 tool_result NativeMessage 并匹配 tool_use_id。

        如果 outcome 是 ToolFailure，自动设置 is_error: true。
        如果 tool_use_id 不匹配任何 pending tool_use，仍然生成带 is_error 的
        tool_result（CC 错误兜底）。

        StepLoop 只需传入 outcome + tool_call — 无需知道 tool_use_id 格式。
        """
        tool_use_id = tool_call.id

        if isinstance(outcome, ToolSuccess):
            block = ToolResultBlock.from_success(
                tool_use_id,
                outcome.output or "",
            )
        elif isinstance(outcome, ToolFailure):
            block = ToolResultBlock.from_error(
                tool_use_id,
                outcome.error or "Tool execution failed",
            )
        elif isinstance(outcome, ToolDenied):
            block = ToolResultBlock.from_denied(
                tool_use_id,
                outcome.reason or "Tool call denied",
            )
        else:
            # 未知 outcome 类型 → 兜底
            block = ToolResultBlock.from_error(
                tool_use_id,
                f"Unknown tool outcome: {type(outcome).__name__}",
            )

        # 匹配 pending tool_use（用于错误兜底）
        if tool_use_id in self._pending_tool_uses:
            del self._pending_tool_uses[ tool_use_id]

        # tool_result 以 user role 发送（Anthropic API 要求）
        msg = NativeMessage(role="user", content=(block,))
        self._messages.append(msg)

    def add_raw_message(self, msg: NativeMessage) -> None:
        """直接追加 NativeMessage（用于从 DB 恢复）。"""
        self._messages.append(msg)
        # 恢复 pending tool_uses 状态
        for block in msg.content:
            if isinstance(block, ToolUseBlock) and block.id:
                self._pending_tool_uses[block.id] = block
            elif isinstance(block, ToolResultBlock) and block.tool_use_id:
                self._pending_tool_uses.pop(block.tool_use_id, None)

    # ── Queries ──────────────────────────────────────────────────────────

    def to_conversation(self) -> NativeConversation:
        """导出为不可变 NativeConversation（供 Backend.invoke 使用）。"""
        return NativeConversation(messages=tuple(self._messages))

    def to_api_format(self) -> list[dict]:
        """直接输出 Anthropic API 格式 — 绕过 LLMMessage 翻译。"""
        return self.to_conversation().to_api_format()

    @property
    def messages(self) -> tuple[NativeMessage, ...]:
        """当前所有消息（不可变视图）。"""
        return tuple(self._messages)

    @property
    def last_message(self) -> NativeMessage | None:
        """最后一条消息。"""
        return self._messages[-1] if self._messages else None

    @property
    def pending_tool_use_count(self) -> int:
        """未完成的 tool_use 数量（用于验证协议完整性）。"""
        return len(self._pending_tool_uses)

    def has_pending_tool_uses(self) -> bool:
        """是否有未收到 tool_result 的 tool_use。"""
        return len(self._pending_tool_uses) > 0

    # ── Recovery ─────────────────────────────────────────────────────────

    @classmethod
    def rebuild_from(cls, conversation: NativeConversation) -> "ConversationState":
        """从 NativeConversation 重建状态（用于崩溃恢复）。

        遍历所有消息，重建 _pending_tool_uses 索引。
        """
        state = cls()
        for msg in conversation.messages:
            state.add_raw_message(msg)
        return state

    def drain_pending_as_errors(self) -> tuple[NativeMessage, ...]:
        """将所有未匹配的 tool_use 转为 is_error tool_result。

        用于：Run 结束时确保协议完整性（每个 tool_use 都有 tool_result）。
        """
        error_msgs: list[NativeMessage] = []
        for tool_use_id in list(self._pending_tool_uses.keys()):
            block = ToolResultBlock.from_error(
                tool_use_id,
                "Tool execution interrupted (no result received)",
            )
            msg = NativeMessage(role="user", content=(block,))
            self._messages.append(msg)
            error_msgs.append(msg)
            del self._pending_tool_uses[tool_use_id]
        return tuple(error_msgs)

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _extract_thought(action: ToolCall | ToolCallBatch) -> str:
        """Extract thought text from a tool call action."""
        if isinstance(action, ToolCall):
            # ToolCall doesn't carry thought text directly
            return ""
        # ToolCallBatch — 也无 thought
        return ""
