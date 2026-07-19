"""agent/agent_config.py

Agent 运行时配置，独立于 ReActAgent 主循环。
从 agent/core.py 提取。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from hooks.events import HookEvent
from llm.base import LLMMessage

if TYPE_CHECKING:
    from agent.completion_guard import CompletionCheckResult


@dataclass
class AgentConfig:
    """Agent 运行时配置，从 config/default.yaml 加载后传入。"""
    max_steps: int = 40
    budget_tokens: int = 160_000
    request_budget_tokens: int = 110_000
    artifact_threshold_tokens: int = 2_000
    artifact_storage_dir: str = ""
    missing_test_target_max_followups: int = 2
    max_parallel_tool_calls: int = 3
    history_max_messages: int = 200
    llm_max_retries: int = 3
    llm_retry_delay: float = 2.0
    stream: bool = False
    stream_callback: object = None
    thought_callback: object = None
    token_callback: Callable[[int], None] | None = None
    cancellation_token: "Any | None" = None
    completion_fact_check: "Callable[[], CompletionCheckResult] | None" = None
    verify_callback: "Callable[[], CompletionCheckResult] | None" = None
    runtime_message_source: Callable[[], list[LLMMessage]] | None = None
    stop_hook_event: HookEvent = HookEvent.STOP
    hook_session_id: str = ""
    hook_agent_id: str = ""
    hook_agent_type: str = ""
    stats_session_id: str = ""
    stats_agent_name: str = ""
    hook_dispatcher: object = None
    confirm_dangerous: bool = False
    effort: str = ""
    confirm_callback: object = None
    compact_history: bool = True
    is_subagent: bool = False
    circuit_breaker: object = None
    streaming_tool_execution: bool = False
    token_budget_continuation: bool = False
    session_notes: bool = False
    stats_collector: object = None
    """First-party stats collector — called directly from agent loop.
    Records tool calls, session lifecycle, and LLM token usage.
    NOT an EventBus side effect."""
