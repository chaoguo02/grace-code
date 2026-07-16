"""
tests/test_context.py

context 管理模块单元测试：
- ArtifactStore (context/artifacts.py)
- ContextManager (context/manager.py)
- SessionState (context/session.py)
"""

from __future__ import annotations

import pytest


class TestConversationSnapshot:
    def test_capture_is_deeply_immutable_and_materializes_fresh_objects(self):
        from agent.task import ToolCall
        from context.history import ConversationSnapshot
        from llm.base import LLMMessage

        content = [{"type": "text", "text": "before"}]
        params = {"path": "a.py", "options": ["one"]}
        messages = [
            LLMMessage(role="system", content=content),
            LLMMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(name="file_read", params=params, id="call-1")],
            ),
            LLMMessage(role="tool", content="result", tool_call_id="call-1"),
        ]

        snapshot = ConversationSnapshot.capture(messages)
        fingerprint = snapshot.fingerprint
        content[0]["text"] = "after"
        params["options"].append("two")

        first = snapshot.materialize()
        second = snapshot.materialize()
        assert first[0].content[0]["text"] == "before"
        assert first[1].tool_calls[0].params == {
            "options": ["one"], "path": "a.py",
        }
        assert first[0] is not second[0]
        assert first[1].tool_calls[0] is not second[1].tool_calls[0]
        assert snapshot.fingerprint == fingerprint

    def test_rejects_orphan_tool_result(self):
        from context.history import ConversationSnapshot, ConversationSnapshotError
        from llm.base import LLMMessage

        with pytest.raises(ConversationSnapshotError, match="not paired"):
            ConversationSnapshot.capture([
                LLMMessage(role="tool", content="orphan", tool_call_id="call-1"),
            ])

    def test_rejects_incomplete_tool_call_sequence(self):
        from agent.task import ToolCall
        from context.history import ConversationSnapshot, ConversationSnapshotError
        from llm.base import LLMMessage

        with pytest.raises(ConversationSnapshotError, match="ends before"):
            ConversationSnapshot.capture([
                LLMMessage(
                    role="assistant",
                    content="",
                    tool_calls=[ToolCall(name="file_read", params={}, id="call-1")],
                ),
            ])

    def test_rejects_non_contiguous_tool_results(self):
        from agent.task import ToolCall
        from context.history import ConversationSnapshot, ConversationSnapshotError
        from llm.base import LLMMessage

        with pytest.raises(ConversationSnapshotError, match="contiguous"):
            ConversationSnapshot.capture([
                LLMMessage(
                    role="assistant",
                    content="",
                    tool_calls=[ToolCall(name="file_read", params={}, id="call-1")],
                ),
                LLMMessage(role="user", content="interruption"),
            ])


# ===========================================================================
# ArtifactStore 测试
# ===========================================================================


class TestArtifactStore:
    """ArtifactStore 的核心行为测试。"""

    def _make_store(self, threshold_tokens=100, max_artifacts=5):
        from context.artifacts import ArtifactStore
        return ArtifactStore(
            threshold_tokens=threshold_tokens,
            max_artifacts=max_artifacts,
            summary_lines=3,
            summary_tail_lines=2,
        )

    def test_small_output_not_stored(self):
        """小于阈值的输出直接返回，不被 artifact 化。"""
        store = self._make_store(threshold_tokens=1000)
        text, was_stored = store.maybe_store("shell", "hello world")
        assert was_stored is False
        assert text == "hello world"
        assert store.count == 0

    def test_large_output_stored(self):
        """超过阈值的输出被 artifact 化，返回摘要引用。"""
        store = self._make_store(threshold_tokens=10)
        big_output = "\n".join(f"line {i}: some content here" for i in range(100))
        text, was_stored = store.maybe_store("shell", big_output)
        assert was_stored is True
        assert "[Artifact" in text
        assert "shell" in text
        assert store.count == 1

    def test_empty_output_not_stored(self):
        """空输出直接返回。"""
        store = self._make_store(threshold_tokens=10)
        text, was_stored = store.maybe_store("shell", "")
        assert was_stored is False
        assert text == ""

    def test_get_by_id(self):
        """按 ID 获取 artifact 完整内容。"""
        store = self._make_store(threshold_tokens=10)
        big_output = "x" * 500
        store.maybe_store("shell", big_output)
        artifacts = store.list_artifacts()
        assert len(artifacts) == 1
        art_id = artifacts[0][0]
        content = store.get_full_content(art_id)
        assert content == big_output

    def test_get_nonexistent_returns_none(self):
        """获取不存在的 ID 返回 None。"""
        store = self._make_store()
        assert store.get("nonexistent") is None
        assert store.get_full_content("nonexistent") is None

    def test_lru_eviction(self):
        """超过 max_artifacts 时 LRU 淘汰最旧的条目。"""
        store = self._make_store(threshold_tokens=10, max_artifacts=3)
        outputs = [f"{'x' * 500} item {i}" for i in range(5)]
        ids = []
        for i, out in enumerate(outputs):
            store.maybe_store(f"tool_{i}", out)
            arts = store.list_artifacts()
            ids.append(arts[-1][0])

        assert store.count == 3
        # 最早的 2 个应该被淘汰
        assert store.get(ids[0]) is None
        assert store.get(ids[1]) is None
        # 最新的 3 个应该存在
        assert store.get(ids[2]) is not None
        assert store.get(ids[3]) is not None
        assert store.get(ids[4]) is not None

    def test_lru_access_extends_life(self):
        """访问后的 artifact 不会被优先淘汰。"""
        store = self._make_store(threshold_tokens=10, max_artifacts=3)
        # 填满 3 个
        for i in range(3):
            store.maybe_store(f"tool_{i}", f"{'x' * 500} item {i}")
        arts = store.list_artifacts()
        first_id = arts[0][0]

        # 访问第一个（让它变新）
        store.get(first_id)

        # 再加一个，触发淘汰
        store.maybe_store("tool_3", "y" * 500)
        assert store.count == 3
        # 第一个因为被访问应该还在
        assert store.get(first_id) is not None

    def test_total_tokens_stored(self):
        """total_tokens_stored 正确累加。"""
        store = self._make_store(threshold_tokens=10)
        store.maybe_store("shell", "x" * 500)
        store.maybe_store("shell", "y" * 800)
        assert store.total_tokens_stored > 0
        assert store.count == 2

    def test_summary_preserves_head_and_tail(self):
        """摘要保留首 N 行和尾 M 行。"""
        store = self._make_store(threshold_tokens=10, max_artifacts=5)
        lines = [f"LINE_{i:03d}" for i in range(50)]
        big_output = "\n".join(lines)
        text, _ = store.maybe_store("shell", big_output)
        # 首行应该保留
        assert "LINE_000" in text
        # 尾行应该保留
        assert "LINE_049" in text
        # 中间应该被省略
        assert "omitted" in text

    def test_duplicate_content_same_id(self):
        """相同内容（前1000字符）产生相同的 artifact_id。"""
        store = self._make_store(threshold_tokens=10)
        output = "z" * 500
        store.maybe_store("tool_a", output)
        store.maybe_store("tool_b", output)
        # 相同前缀 → 相同 hash → 相同 ID → 覆盖更新
        assert store.count == 1


# ===========================================================================
# TaskRouter 测试
# ===========================================================================


# ===========================================================================
# SessionState 测试
# ===========================================================================


class TestSessionState:
    """SessionState 的状态管理测试。"""

    def test_start_and_finish_task(self):
        """正常的任务开始→结束流程。"""
        from context.session import SessionState, TaskSummary
        state = SessionState()
        ctx = state.start_task("fix auth bug", intent="edit")
        assert state.active_task is ctx
        assert not hasattr(ctx, "relationship")
        assert state.round_count == 1

        summary = TaskSummary(
            task_id=ctx.task_id,
            user_goal="fix auth bug",
            outcome="success",
            changed_files=["src/auth.py"],
        )
        state.finish_task(summary)
        assert state.active_task is None
        assert len(state.completed_tasks) == 1

    def test_rolling_summary_builds(self):
        """完成任务后滚动摘要自动生成。"""
        from context.session import SessionState, TaskSummary
        state = SessionState()
        for i in range(3):
            state.start_task(f"task {i}")
            state.finish_task(TaskSummary(user_goal=f"task {i}", outcome="done"))

        assert state.rolling_summary != ""
        assert "task 0" in state.rolling_summary
        assert "task 2" in state.rolling_summary

    def test_rolling_summary_keeps_last_5(self):
        """滚动摘要只保留最近 5 个任务。"""
        from context.session import SessionState, TaskSummary
        state = SessionState()
        for i in range(8):
            state.start_task(f"task {i}")
            state.finish_task(TaskSummary(user_goal=f"task {i}", outcome="done"))

        # 最旧的任务不在摘要中
        assert "task 0" not in state.rolling_summary
        assert "task 1" not in state.rolling_summary
        assert "task 2" not in state.rolling_summary
        # 最近的在
        assert "task 7" in state.rolling_summary

    def test_get_session_context_respects_budget(self):
        """get_session_context_for_prompt 尊重 token 预算。"""
        from context.session import SessionState, TaskSummary
        state = SessionState()
        for i in range(10):
            state.start_task(f"task {i} with a really long description " * 10)
            state.finish_task(TaskSummary(
                user_goal=f"task {i} with a really long description " * 10,
                outcome="done with lots of detail " * 5,
            ))

        # 极小预算应该只返回少量内容
        result = state.get_session_context_for_prompt(budget_tokens=50)
        # 应该有内容但不会超出预算太多
        assert result == "" or len(result) < 2000

    def test_empty_session_returns_empty(self):
        """无已完成任务时返回空字符串。"""
        from context.session import SessionState
        state = SessionState()
        assert state.get_session_context_for_prompt() == ""
        assert state.estimated_tokens() == 0

    def test_task_summary_to_text(self):
        """TaskSummary.to_text() 生成紧凑格式。"""
        from context.session import TaskSummary
        summary = TaskSummary(
            user_goal="fix login",
            outcome="fixed",
            changed_files=["auth.py"],
            commands=["pytest"],
            decisions=["used bcrypt"],
        )
        text = summary.to_text()
        assert "fix login" in text
        assert "auth.py" in text
        assert "pytest" in text
        assert "bcrypt" in text


# ===========================================================================
# ContextManager 测试
# ===========================================================================


class TestContextManager:
    """ContextManager 的组装逻辑测试。"""

    def _make_history(self, messages=None):
        from context.history import ConversationHistory
        from llm.base import LLMMessage
        history = ConversationHistory(max_messages=100)
        if messages:
            for msg in messages:
                history.add(LLMMessage(role=msg["role"], content=msg["content"]))
        return history

    def _make_budget(self):
        from context.token_budget import TokenBudget
        return TokenBudget(total=100_000)

    def test_basic_build(self):
        """基本组装：system + history → messages。"""
        from context.manager import ContextManager
        mgr = ContextManager()
        history = self._make_history([
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ])
        ctx = mgr.build_request_messages(
            history=history,
            token_budget=self._make_budget(),
            system_core_text="You are a helpful assistant.",
        )
        assert len(ctx.messages) >= 3  # system + user + assistant
        assert ctx.messages[0].role == "system"
        assert ctx.stats.estimated_total_tokens > 0

    def test_long_term_context_injection(self):
        """long_term_context 被注入为独立消息对。"""
        from context.manager import ContextManager
        mgr = ContextManager()
        history = self._make_history([{"role": "user", "content": "do something"}])
        ctx = mgr.build_request_messages(
            history=history,
            token_budget=self._make_budget(),
            system_core_text="system",
            long_term_context="## Memory\nsome project knowledge",
        )
        # 应该有 system + long_term user + ack assistant + user
        contents = [m.content for m in ctx.messages]
        assert any("Memory" in (c or "") for c in contents)
        assert any("Understood" in (c or "") for c in contents)

    def test_task_anchor_appended(self):
        """task_anchor 被追加为最后一条消息。"""
        from context.manager import ContextManager
        mgr = ContextManager()
        history = self._make_history([{"role": "user", "content": "fix bug"}])
        ctx = mgr.build_request_messages(
            history=history,
            token_budget=self._make_budget(),
            system_core_text="system",
            task_anchor="## Current Task\nfix the auth bug",
        )
        last_msg = ctx.messages[-1]
        assert "Current Task" in (last_msg.content or "")

    def test_compaction_triggered(self):
        """should_compact_fn 返回 True 时触发 compaction。"""
        from context.manager import ContextManager
        mgr = ContextManager()
        history = self._make_history([
            {"role": "user", "content": f"message {i} " * 100}
            for i in range(20)
        ])

        compacted = False

        def fake_compactor(dicts):
            nonlocal compacted
            compacted = True
            return dicts[:3]

        ctx = mgr.build_request_messages(
            history=history,
            token_budget=self._make_budget(),
            system_core_text="system",
            compactor_fn=fake_compactor,
            should_compact_fn=lambda dicts, budget: True,
        )
        assert compacted is True
        assert ctx.compact_triggered is True

    def test_compaction_not_triggered_when_fn_false(self):
        """should_compact_fn 返回 False 时不触发。"""
        from context.manager import ContextManager
        mgr = ContextManager()
        history = self._make_history([{"role": "user", "content": "short"}])

        compacted = False

        def fake_compactor(dicts):
            nonlocal compacted
            compacted = True
            return dicts

        ctx = mgr.build_request_messages(
            history=history,
            token_budget=self._make_budget(),
            system_core_text="system",
            compactor_fn=fake_compactor,
            should_compact_fn=lambda dicts, budget: False,
        )
        assert compacted is False
        assert ctx.compact_triggered is False

    def test_sub_agent_messages_no_trim(self):
        """sub_agent 模式不做裁剪。"""
        from context.manager import ContextManager
        mgr = ContextManager()
        history = self._make_history([
            {"role": "user", "content": f"msg {i}"} for i in range(30)
        ])
        ctx = mgr.build_sub_agent_messages(
            history=history,
            system_content="sub-agent system prompt",
        )
        # system + 30 history messages
        assert len(ctx.messages) == 31
        assert ctx.messages[0].content == "sub-agent system prompt"

    def test_stats_populated(self):
        """stats 的各字段被正确填充。"""
        from context.manager import ContextManager
        mgr = ContextManager()
        history = self._make_history([
            {"role": "user", "content": "hello " * 50},
            {"role": "assistant", "content": "response " * 50},
        ])
        ctx = mgr.build_request_messages(
            history=history,
            token_budget=self._make_budget(),
            system_core_text="system prompt " * 20,
            long_term_context="memory content " * 10,
            repo_map_text="repo map " * 10,
        )
        assert ctx.stats.system_tokens > 0
        assert ctx.stats.memory_tokens > 0
        assert ctx.stats.repo_map_tokens > 0
        assert ctx.stats.estimated_total_tokens > 0
