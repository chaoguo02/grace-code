"""Tests for the single-owner in-turn context reduction path."""

from __future__ import annotations

import agent.context_trimming as trimming
from agent.context_trimming import ContextTrimmingState, prepare_history_for_turn
from agent.core import ReActAgent
from context.collapse import CollapseEntry, CollapseStore
from context.history import ConversationHistory
from context.compaction import CompactionMethod, ConversationCompactor
from context.manager import (
    ContextBudget,
    ContextManager,
    ContextPlanner,
    ContextPlanningStage,
    ContextReduction,
    ContextSnapshot,
)
from context.token_budget import TokenBudget
from llm.base import LLMMessage


class _RecordingCompactor:
    def __init__(self) -> None:
        self.compact_args = None

    def compact_history(
        self,
        history_dicts,
        max_tokens=None,
        task_context="",
    ):
        self.compact_args = (
            list(history_dicts),
            max_tokens,
            task_context,
        )
        return [
            history_dicts[0],
            {
                "role": "user",
                "kind": "compaction_boundary",
                "content": "[Conversation compacted]\ncritical summary",
            },
        ]


def test_pre_provider_trimming_pipeline_owns_order_and_state(monkeypatch) -> None:
    history = ConversationHistory()
    state = ContextTrimmingState()
    calls: list[str] = []

    monkeypatch.setattr(
        trimming,
        "_apply_tool_result_budget",
        lambda history, *, budget_state: calls.append("budget") or 10,
    )
    monkeypatch.setattr(
        trimming,
        "_structural_compact",
        lambda history: calls.append("structural") or 50,
    )

    def _collapse(history, compactor, *, collapse_store):
        calls.append("collapse")
        assert collapse_store is None
        return 40, "persisted-store"

    monkeypatch.setattr(trimming, "_apply_context_collapse", _collapse)
    planner = ContextPlanner(
        collapse_ratio=0,
        min_collapse_messages=0,
    )

    first = prepare_history_for_turn(
        history,
        object(),
        step=1,
        enabled=True,
        history_budget=1000,
        state=state,
        planner=planner,
    )
    second = prepare_history_for_turn(
        history,
        object(),
        step=2,
        enabled=True,
        history_budget=1000,
        state=state,
        planner=planner,
    )

    assert first.tokens_freed == 0
    assert second.tokens_freed == 100
    # Phase 3: Snip + Micro merged into StructuralCompactor — one call instead of two
    assert calls == ["budget", "structural", "collapse"]
    assert state.collapse_store == "persisted-store"


def test_react_projection_reads_aggregated_trimming_state() -> None:
    history = ConversationHistory()
    history.add(LLMMessage(role="user", content="old request"))
    history.add(LLMMessage(role="assistant", content="old response"))
    history.add(LLMMessage(role="user", content="current request"))
    store = CollapseStore([
        CollapseEntry(start=0, end=2, summary="prior work"),
    ])
    agent = object.__new__(ReActAgent)
    agent._context_trimming_state = ContextTrimmingState(
        collapse_store=store,
    )

    projected = agent._apply_collapse_projection(history)

    assert len(projected.messages) == 2
    assert "prior work" in projected.messages[0].content
    assert projected.messages[1].content == "current request"


def test_context_manager_owns_compaction_decision_and_reports_summary() -> None:
    history = ConversationHistory()
    history.add(LLMMessage(role="user", content="original task"))
    history.add(LLMMessage(role="assistant", content="old analysis"))
    compactor = _RecordingCompactor()

    planner = ContextPlanner(
        compact_ratio=0,
        min_compact_messages=2,
        compact_cooldown_steps=0,
    )
    result = ContextManager(planner=planner).build_request_messages(
        history=history,
        token_budget=TokenBudget(total=20_000),
        system_core_text="system",
        compactor=compactor,
        compaction_task_context="current task",
        tokens_freed=321,
    )

    assert result.compact_triggered
    assert result.compaction_summary == (
        "[Conversation compacted]\ncritical summary"
    )
    assert compactor.compact_args is not None
    assert compactor.compact_args[2] == "current task"
    assert all(
        "old analysis" not in str(message.content)
        for message in result.messages
    )


def test_context_planner_is_single_owner_of_reduction_thresholds() -> None:
    planner = ContextPlanner(
        collapse_ratio=0.50,
        compact_ratio=0.80,
        min_collapse_messages=4,
        min_compact_messages=4,
        compact_cooldown_steps=2,
    )
    budget = ContextBudget(history_tokens=1000)

    prepare = planner.plan(
        ContextSnapshot(
            message_count=4,
            estimated_tokens=600,
            step=2,
        ),
        budget,
        stage=ContextPlanningStage.PREPARE,
    )
    assert prepare.reductions == (
        ContextReduction.TOOL_RESULT_BUDGET,
        ContextReduction.SNIP,
        ContextReduction.MICRO_COMPACT,
        ContextReduction.COLLAPSE,
    )

    compact = planner.plan(
        ContextSnapshot(
            message_count=4,
            estimated_tokens=900,
            step=2,
        ),
        budget,
        stage=ContextPlanningStage.ASSEMBLE,
    )
    assert compact.reductions == (ContextReduction.COMPACT,)

    planner.record_compaction()
    cooling_down = planner.plan(
        ContextSnapshot(
            message_count=4,
            estimated_tokens=900,
            step=3,
        ),
        budget,
        stage=ContextPlanningStage.ASSEMBLE,
    )
    assert not cooling_down.reductions


def test_public_compaction_result_reports_regex_truncation() -> None:
    compactor = ConversationCompactor(backend=None)
    messages = [
        {
            "role": "assistant",
            "content": "Thought: " + ("important detail " * 200),
        },
        {
            "role": "user",
            "content": "[Tool: Read | SUCCESS]\n" + ("output " * 300),
        },
    ]

    result = compactor.summarize(messages, max_tokens=10)

    assert result.method is CompactionMethod.REGEX
    assert result.truncated
    assert result.source_range == (0, 2)
    assert result.text.endswith("... (truncated to fit budget)")


def test_semantic_compaction_uses_shared_provider_capacity_and_reports_usage() -> None:
    class _Limiter:
        def __init__(self) -> None:
            self.acquired = []
            self.released = []

        def acquire_wait(self, *, tokens, timeout_s, cancellation_token=None):
            self.acquired.append((tokens, timeout_s, cancellation_token))
            return True

        def release(self, tokens_used=0, *, reserved_tokens=0):
            self.released.append((tokens_used, reserved_tokens))

    class _Response:
        raw_content = "critical summary"
        total_tokens = 321

    class _Backend:
        timeout_seconds = 12

        def __init__(self) -> None:
            self._provider_limiter = _Limiter()

        def complete(self, messages, tools):
            assert len(messages) == 2
            assert tools == []
            return _Response()

    backend = _Backend()
    usage = []
    compactor = ConversationCompactor(
        backend=backend,
        usage_callback=usage.append,
    )
    result = compactor.summarize([
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
    ], max_tokens=2_000)

    assert result.text == "critical summary"
    assert usage == [321]
    assert len(backend._provider_limiter.acquired) == 1
    assert backend._provider_limiter.released == [
        (321, backend._provider_limiter.acquired[0][0]),
    ]
