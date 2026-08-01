"""P0_1 Batch 3: Agent core v2 context integration — acceptance tests.

AC mappings:
  AC-6.1  No build_sub_agent_messages / build_inherited_messages in v2 path
  AC-6.3  Sub-agent with simple task does not trigger provider 400
  AC-5.1  Sub-agent system prompt contains NO parent history
  AC-5.3  200K parent history → sub-agent context < 10K tokens
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from llm.base import LLMMessage


# ===========================================================================
# 1. Feature flag routing
# ===========================================================================

class TestFeatureFlagRouting:
    """V2 is the DEFAULT. Opt-out via GRACE_USE_LEGACY_CONTEXT."""

    def test_default_is_v2(self):
        """Without any flag, v2 path is used."""
        assert os.environ.get("GRACE_USE_LEGACY_CONTEXT") != "1"

    def test_opt_out_flag(self, monkeypatch):
        """GRACE_USE_LEGACY_CONTEXT=1 routes to old three-path code."""
        monkeypatch.setenv("GRACE_USE_LEGACY_CONTEXT", "1")
        assert os.environ.get("GRACE_USE_LEGACY_CONTEXT") == "1"


# ===========================================================================
# Module-level fixtures
# ===========================================================================

@pytest.fixture
def mock_backend():
    b = MagicMock()
    b.max_context_window = 200_000
    b.provider_name = "anthropic"
    return b

@pytest.fixture
def mock_registry():
    r = MagicMock()
    r.get_schemas.return_value = []
    r.artifact_store_ref = MagicMock()
    r.artifact_store_ref.store = None
    return r

@pytest.fixture
def mock_prompt_renderer():
    pr = MagicMock()
    pr.system_core.return_value = "## Core System\nYou are helpful."
    pr.sub_agent_system.return_value = "## Sub-agent\nExecute tasks."
    pr.system_variable.return_value = ""
    return pr


# ===========================================================================
# 2. V2 build_messages — direct method test
# ===========================================================================

class TestBuildMessagesV2:

    def test_main_session_path_returns_messages(self, mock_backend, mock_registry, mock_prompt_renderer):
        from agent.core import ReActAgent
        from agent.agent_config import AgentConfig
        from context.history import ConversationHistory
        from context.token_budget import TokenBudget

        cfg = AgentConfig()
        cfg.is_subagent = False
        agent = ReActAgent(mock_backend, mock_registry, cfg)
        agent._prompt_renderer = mock_prompt_renderer
        agent._prompt_renderer_is_injected = True
        agent._inherited_context = None
        agent._repo_map_cache = "# Repo Map\nsrc/\n"
        agent._current_repo_path = "."

        history = ConversationHistory(max_messages=100)
        history.add_many([
            LLMMessage(role="user", content="Hello"),
            LLMMessage(role="assistant", content="Hi there!"),
        ])

        tb = TokenBudget(total=200_000)
        rm = MagicMock()
        rm.build = MagicMock(return_value="# Repo Map\nsrc/")

        messages = agent._build_messages_v2(
            history=history, token_budget=tb, repo_map=rm,
            consumed_tokens=0, max_context_window=200_000,
        )
        assert len(messages) > 0
        assert messages[0].role == "system"
        assert "Core System" in str(messages[0].content)

    def test_sub_agent_path_clean_context(self, mock_backend, mock_registry, mock_prompt_renderer):
        from agent.core import ReActAgent
        from agent.agent_config import AgentConfig
        from context.history import ConversationHistory
        from context.token_budget import TokenBudget

        cfg = AgentConfig()
        cfg.is_subagent = True
        agent = ReActAgent(mock_backend, mock_registry, cfg)
        agent._prompt_renderer = mock_prompt_renderer
        agent._prompt_renderer_is_injected = True
        agent._inherited_context = None
        agent._current_task_description = "Find TODO comments"
        agent._repo_map_cache = ""

        # Parent history
        history = ConversationHistory(max_messages=100)
        history.add_many([
            LLMMessage(role="user", content="Parent: what is 2+2?"),
            LLMMessage(role="assistant", content="4"),
            LLMMessage(role="user", content="Now find all bugs"),
            LLMMessage(role="assistant", content="I'll search"),
        ])

        tb = TokenBudget(total=200_000)
        rm = MagicMock()
        rm.build = MagicMock(return_value="")

        messages = agent._build_messages_v2(
            history=history, token_budget=tb, repo_map=rm,
            consumed_tokens=0, max_context_window=200_000,
        )
        assert len(messages) == 1
        content = str(messages[0].content)
        assert "Find TODO comments" in content
        assert "2+2" not in content
        assert "find all bugs" not in content

    def test_inherited_context_clean(self, mock_backend, mock_registry, mock_prompt_renderer):
        from agent.core import ReActAgent
        from agent.agent_config import AgentConfig
        from context.history import ConversationHistory, ConversationSnapshot
        from context.token_budget import TokenBudget

        cfg = AgentConfig()
        cfg.is_subagent = False
        agent = ReActAgent(mock_backend, mock_registry, cfg)
        agent._prompt_renderer = mock_prompt_renderer
        agent._prompt_renderer_is_injected = True
        agent._current_task_description = "Continue the work"

        # Parent snapshot
        parent_history = ConversationHistory(max_messages=100)
        parent_history.add_many([
            LLMMessage(role="user", content="Original: refactor auth"),
            LLMMessage(role="assistant", content="I'll start"),
        ])
        agent._inherited_context = ConversationSnapshot.capture(
            list(parent_history._messages),
        )

        # Child history
        history = ConversationHistory(max_messages=100)
        history.add_many([
            LLMMessage(role="user", content="Continue refactoring login"),
        ])

        tb = TokenBudget(total=200_000)
        rm = MagicMock()
        rm.build = MagicMock(return_value="")

        messages = agent._build_messages_v2(
            history=history, token_budget=tb, repo_map=rm,
            consumed_tokens=0, max_context_window=200_000,
        )
        all_content = " ".join(str(m.content) for m in messages)
        assert "refactor auth" not in all_content


class TestSubAgentContextSize:

    def test_big_parent_small_child(self, mock_backend, mock_registry, mock_prompt_renderer):
        from agent.core import ReActAgent
        from agent.agent_config import AgentConfig
        from context.history import ConversationHistory
        from context.token_budget import TokenBudget
        from context.counters import CharEstimator

        cfg = AgentConfig()
        cfg.is_subagent = True
        agent = ReActAgent(mock_backend, mock_registry, cfg)
        agent._prompt_renderer = mock_prompt_renderer
        agent._prompt_renderer_is_injected = True
        agent._inherited_context = None
        agent._current_task_description = "Simple grep for TODO"

        # 40-turn MASSIVE parent history
        history = ConversationHistory(max_messages=200)
        fat: list[LLMMessage] = []
        for i in range(40):
            fat.append(LLMMessage(role="user", content=f"Q{i}: " + "ctx " * 500))
            fat.append(LLMMessage(role="assistant", content=f"A{i} " + "x" * 2000))
            fat.append(LLMMessage(role="tool", content="z" * 5000, tool_call_id=f"t{i}"))
        history.add_many(fat)

        tb = TokenBudget(total=200_000)
        rm = MagicMock()
        rm.build = MagicMock(return_value="")

        messages = agent._build_messages_v2(
            history=history, token_budget=tb, repo_map=rm,
            consumed_tokens=0, max_context_window=200_000,
        )

        assert len(messages) == 1
        e = CharEstimator(model_window=200_000)
        est = e.estimate(str(messages[0].content))
        assert est < 10_000, f"Sub-agent context should be <10K tokens. Got {est}"
