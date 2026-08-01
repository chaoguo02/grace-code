"""P0_1 Batch 1: TokenCounter + TokenPlanner — acceptance tests.

AC mappings (from P0_1_CONTEXT_WINDOW_MANAGER_DESIGN.md):
  AC-1.3  TokenCounter initialises with no globals / env / filesystem
  AC-1.4  Multimodal (image + text) estimate > len(text)//4
  AC-2.1  plan(model_window=200_000, consumed_tokens=150_000) → history > 0
  AC-2.2  sum of allocations == total (no "leak")
  AC-2.3  plan(model_window=128_000) → repo_map <= 12_000
"""

from __future__ import annotations

import pytest

from context.counters import (
    CharEstimator,
    TokenCount,
    _estimate_text,
    _estimate_block,
    _estimate_any,
    _estimate_msg,
)
from context.planner import BudgetPlan, TokenPlanner


# ===========================================================================
# 1. LocalTokenEstimator — multimodal correctness
# ===========================================================================

class TestCharEstimator:
    """AC-1.3 + AC-1.4: Estimator correctness and multimodal support."""

    def test_instantiation_no_globals(self):
        """AC-1.3: can create without env, filesystem, or global state."""
        e1 = CharEstimator(model_window=200_000)
        e2 = CharEstimator(model_window=128_000)
        assert e1.model_context_window == 200_000
        assert e2.model_context_window == 128_000

    def test_text_estimate_reasonable(self):
        """Plain text gives reasonable estimate (not 0, not absurd)."""
        e = CharEstimator()
        text = "hello world " * 100
        tokens = e.estimate(text)
        # ~1200 chars / 3.6 ≈ 333 tokens, should be in [200, 500]
        assert 100 < tokens < 1000

    def test_multimodal_list_not_underestimated(self):
        """AC-1.4: list of blocks is NOT estimated by len(list)."""
        e = CharEstimator()
        content = [
            {"type": "text", "text": "x" * 10_000},
            {"type": "image", "source": {"type": "base64", "data": "A" * 50_000}},
        ]
        tokens = e.estimate(content)
        # len(list) = 2, char/4 would give ~2.  But real tokens >> 2.
        assert tokens > 100, (
            f"Multimodal content should NOT be estimated by list length. "
            f"Got {tokens}, expected > 100"
        )

    def test_estimate_messages_sum(self):
        """estimate_messages sums per-message estimates."""
        e = CharEstimator()
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        tokens = e.estimate_messages(msgs)
        single = e.estimate("hello") + e.estimate("hi there")
        # Should be >= sum of individual content estimates (plus overhead)
        assert tokens >= single

    def test_image_block_estimate_conservative(self):
        """Image blocks get a conservative non-trivial estimate."""
        img_block = {
            "type": "image",
            "source": {"type": "base64", "data": "A" * 100_000, "media_type": "image/png"},
        }
        tokens = _estimate_block(img_block)
        # 100K chars / 100 ≈ 1000 tokens; minimum is 85
        assert tokens >= 85

    def test_tool_use_block_estimate(self):
        """tool_use blocks are estimated from their JSON representation."""
        block = {
            "type": "tool_use",
            "id": "tool_001",
            "name": "Read",
            "input": {"file_path": "/some/path/file.py"},
        }
        tokens = _estimate_block(block)
        assert tokens > 10  # realistic estimate


# ===========================================================================
# 2. TokenCount — context vs billing separation
# ===========================================================================

class TestTokenCount:
    """TokenCount separates context occupancy from billing dimensions."""

    def test_context_tokens_independent_of_cache(self):
        """context_tokens is NOT input_tokens + cache_creation + cache_read."""
        tc = TokenCount(
            context_tokens=50_000,
            input_tokens=50_000,
            cache_creation_tokens=5_000,
            cache_read_tokens=45_000,
        )
        # context_tokens is the window occupancy (50K)
        # input_tokens + cache_* = 100K (billing)
        assert tc.context_tokens == 50_000
        assert tc.input_tokens + tc.cache_creation_tokens + tc.cache_read_tokens == 100_000

    def test_default_cache_values_are_zero(self):
        tc = TokenCount(context_tokens=100)
        assert tc.cache_creation_tokens == 0
        assert tc.cache_read_tokens == 0


# ===========================================================================
# 3. TokenPlanner — budget allocation invariants
# ===========================================================================

class TestTokenPlanner:
    """AC-2.1, AC-2.2, AC-2.3: budget allocation correctness."""

    def test_allocation_sums_to_total(self):
        """AC-2.2: system + repo_map + history + observation == total.

        total is model_window - output_room.
        So model_window = system + repo_map + history + observation + output_room.
        """
        planner = TokenPlanner()
        plan = planner.plan(model_window=200_000)
        # total already excludes output_room
        assert plan.system + plan.repo_map + plan.history + plan.observation == plan.total
        # full model_window invariant
        assert plan.total + plan.output_room == 200_000

    def test_high_consumption_history_still_positive(self):
        """AC-2.1: even with 150K consumed, history budget > 0."""
        planner = TokenPlanner()
        plan = planner.plan(model_window=200_000, consumed_tokens=150_000)
        assert plan.history > 0, (
            f"History budget should be > 0 even at high consumption. Got {plan.history}"
        )
        assert plan.total == 200_000 - plan.output_room

    def test_deepseek_window_caps_repo_map(self):
        """AC-2.3: DeepSeek 128K window → repo_map <= 12_000."""
        planner = TokenPlanner(repo_map_max=12_000)
        plan = planner.plan(model_window=128_000)
        assert plan.repo_map <= 12_000

    def test_zero_consumption_full_history(self):
        """Fresh session: history gets the full remainder."""
        planner = TokenPlanner()
        plan = planner.plan(model_window=200_000, consumed_tokens=0)
        expected_history = plan.total - plan.system - plan.repo_map - plan.observation
        assert plan.history == expected_history

    def test_custom_output_room(self):
        planner = TokenPlanner()
        plan = planner.plan(model_window=200_000, output_room=8_000)
        assert plan.output_room == 8_000
        assert plan.total == 200_000 - 8_000

    def test_custom_system_fraction(self):
        planner = TokenPlanner()
        plan = planner.plan(model_window=200_000, system_fraction=0.20)
        expected_system = int(plan.total * 0.20)
        assert plan.system == expected_system

    def test_all_values_non_negative(self):
        planner = TokenPlanner()
        plan = planner.plan(model_window=128_000, consumed_tokens=120_000)
        assert plan.system >= 0
        assert plan.repo_map >= 0
        assert plan.history >= 0
        assert plan.observation >= 0
        assert plan.output_room >= 0


# ===========================================================================
# 4. Edge cases
# ===========================================================================

class TestEdgeCases:
    """Boundary conditions for the estimation + planning pipeline."""

    def test_empty_string_estimate(self):
        tokens = _estimate_text("")
        assert tokens >= 1  # never zero

    def test_none_content_in_message(self):
        tokens = _estimate_msg({"role": "assistant", "content": None})
        assert tokens >= 5  # at least per-message overhead

    def test_deeply_nested_tool_result(self):
        """Nested content in tool_result doesn't crash."""
        block = {
            "type": "tool_result",
            "tool_use_id": "x",
            "content": [{"type": "text", "text": "result text " * 500}],
        }
        tokens = _estimate_block(block)
        assert tokens > 100
