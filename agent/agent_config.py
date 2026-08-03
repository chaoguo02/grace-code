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
    from config.schema import PromptConfig


@dataclass
class AgentConfig:
    """Agent 运行时配置，从 config/default.yaml 加载后传入。"""
    max_steps: int = 40
    budget_tokens: int = 160_000
    request_budget_tokens: int = 110_000
    artifact_threshold_tokens: int = 2_000
    artifact_storage_dir: str = ""
    checkpoint_db_path: str = ""
    # ── Phase 1B: per-path write protection (glob patterns) ──
    readonly_paths: list[str] = field(default_factory=list)
    """Additional protected path glob patterns beyond the defaults
    (.git/, .env, __pycache__/, node_modules/, *.lock).  Writes to any
    matching path are rejected by the Write/Edit tools unless explicitly
    allowed via allow_write_to_protected."""
    allow_write_to_protected: list[str] = field(default_factory=list)
    """Explicit overrides (glob patterns) that re-enable writes to
    otherwise-protected paths.  Must be set deliberately by the user."""
    missing_test_target_max_followups: int = 2
    max_parallel_tool_calls: int = 3
    history_max_messages: int = 200
    llm_max_retries: int = 3
    llm_retry_delay: float = 2.0
    request_timeout: float = 300.0
    """Maximum wall-clock seconds for one backend call, including streams."""
    stream: bool = False
    stream_callback: object = None
    thought_callback: object = None
    text_stream_lifecycle_callback: object = None
    """Signature: (event_type: str, block_id: str, reason: str = "") -> None.

    Called for assistant text block lifecycle:
      - ("start", block_id)  — first non-thought TEXT_DELTA
      - ("end", block_id)    — TOOL_USE / FINISH / stream end
      - ("aborted", block_id, reason)  — exception / cancel / max_tokens
    """
    text_stream_delta_callback: object = None
    """Signature: (block_id: str, text: str) -> None.

    Called for each non-thought TEXT_DELTA chunk during streaming.
    """
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
    stats_run_id: str = ""
    stats_turn_id: str = ""
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
    llm_metrics_callback: object | None = None
    memory_event_callback: object | None = None
    """Callback for memory_written runtime events."""
    mode_policy: object | None = None
    """ModeExecutionPolicy — per-Run immutable execution contract.
    Set by SessionRuntime.run_session() and consumed by _initialize_run()
    when creating the RunContext for tool execution."""
    evidence_store: object | None = None
    """RunEvidenceStore — per-root-run evidence aggregator.
    Set by SessionRuntime.run_session()."""
    """Hook-based LLM observability callback (P2-18).
    If set, invoked with a ``RetryMetrics`` dataclass after each LLM
    invocation.  Zero-overhead when None.  Set by AgentService when
    observability is enabled (env FORGE_OBSERVE_RETRIES=1)."""
    prompt_config: "PromptConfig | None" = None
    """Request-scoped prompt assembly configuration."""
