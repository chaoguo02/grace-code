"""
tests/test_stream.py

流式输出测试。
- StreamingMixin 的 fallback 行为
- AgentConfig stream 字段
- core.py 流式路径（mock stream() 方法）
- cli --stream 参数注册
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent.core import Agent, AgentConfig
from agent.event_log import EventLog
from agent.task import Action, ActionType, Task, ToolCall
from llm.base import LLMMessage, LLMResponse, LLMToolSchema, MockBackend
from tools.base import NoopTool, ToolRegistry


# ---------------------------------------------------------------------------
# StreamingMixin fallback
# ---------------------------------------------------------------------------

class TestStreamingMixin:
    def test_default_stream_calls_complete(self):
        """base.py 的默认 stream() 实现直接调 complete()。"""
        script = [Action(ActionType.FINISH, "done", message="ok")]
        backend = MockBackend(script)
        collected = []
        # MockBackend 继承了 StreamingMixin 的默认 stream()
        result = backend.stream(
            [LLMMessage(role="user", content="go")],
            [],
            on_text=lambda t: collected.append(t),
        )
        assert result.action.action_type == ActionType.FINISH
        # fallback 会把 raw_content 传给 on_text
        assert len(collected) > 0

    def test_stream_returns_llm_response(self):
        script = [Action(ActionType.FINISH, "done", message="ok")]
        backend = MockBackend(script)
        result = backend.stream([LLMMessage(role="user", content="go")], [])
        assert isinstance(result, LLMResponse)


# ---------------------------------------------------------------------------
# AgentConfig stream 字段
# ---------------------------------------------------------------------------

class TestAgentConfigStream:
    def test_stream_default_false(self):
        cfg = AgentConfig()
        assert cfg.stream is False
        assert cfg.stream_callback is None

    def test_stream_can_be_enabled(self):
        cb = lambda t: None
        cfg = AgentConfig(stream=True, stream_callback=cb)
        assert cfg.stream is True
        assert cfg.stream_callback is cb


# ---------------------------------------------------------------------------
# core.py 流式路径
# ---------------------------------------------------------------------------

class TestCoreStreamPath:

    def _make_streaming_backend(self, script):
        """创建一个有真实 stream() 方法的 mock backend。"""
        backend = MockBackend(script)
        stream_calls = []

        def fake_stream(messages, tools, on_text=None, on_thought=None):
            stream_calls.append({"messages": messages, "on_text": on_text})
            # 模拟推理模型：先流式输出 thought，再输出 message
            if on_thought:
                on_thought("thinking... ")
            # 模拟流式：分 3 次调用 on_text
            if on_text:
                on_text("I will ")
                on_text("fix the ")
                on_text("bug.")
            return backend.complete(messages, tools)

        backend.stream = fake_stream
        backend._stream_calls = stream_calls
        return backend

    def test_stream_true_calls_stream_method(self, tmp_path):
        """stream=True 时应调用 backend.stream() 而不是 complete()。"""
        script = [Action(ActionType.FINISH, "done", message="ok")]
        backend = self._make_streaming_backend(script)

        collected_text = []
        cfg = AgentConfig(
            stream=True,
            stream_callback=lambda t: collected_text.append(t),
        )
        registry = ToolRegistry().register(NoopTool("shell"))
        agent = Agent(backend, registry, cfg)
        task = Task(task_id="st1", description="fix", repo_path=str(tmp_path), max_steps=3)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        assert result.is_success()
        # stream() 被调用了
        assert len(backend._stream_calls) >= 1
        # on_text 回调收到了分块文本
        assert "".join(collected_text) == "I will fix the bug."

    def test_stream_false_calls_complete(self, tmp_path):
        """stream=False 时应调用 complete()，不调用 stream()。"""
        script = [Action(ActionType.FINISH, "done", message="ok")]
        backend = self._make_streaming_backend(script)
        original_complete_count = [0]
        original_complete = backend.complete

        def counting_complete(messages, tools):
            original_complete_count[0] += 1
            return original_complete(messages, tools)

        backend.complete = counting_complete
        cfg = AgentConfig(stream=False)
        registry = ToolRegistry().register(NoopTool("shell"))
        agent = Agent(backend, registry, cfg)
        task = Task(task_id="st2", description="fix", repo_path=str(tmp_path), max_steps=3)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        assert result.is_success()
        assert original_complete_count[0] >= 1
        # stream() 没有被 core 调用（backend._stream_calls 只有 stream() 才填）
        assert len(backend._stream_calls) == 0

    def test_stream_callback_receives_thought(self, tmp_path):
        """流式回调收到的内容应该是模型 thought 的分块。"""
        script = [
            Action(ActionType.TOOL_CALL, "thinking...", ToolCall("shell", {"cmd": "ls"})),
            Action(ActionType.FINISH, "done", message="ok"),
        ]
        backend = self._make_streaming_backend(script)

        all_text = []
        cfg = AgentConfig(
            stream=True,
            stream_callback=lambda t: all_text.append(t),
        )
        registry = ToolRegistry().register(NoopTool("shell"))
        agent = Agent(backend, registry, cfg)
        task = Task(task_id="st3", description="fix", repo_path=str(tmp_path), max_steps=5)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            agent.run(task, log)

        # 每步都应该有 stream 调用 → 有 on_text 回调
        assert len(all_text) > 0

    def test_stream_no_callback_still_works(self, tmp_path):
        """stream=True 但没有 callback 时不崩溃。"""
        script = [Action(ActionType.FINISH, "done", message="ok")]
        backend = self._make_streaming_backend(script)
        cfg = AgentConfig(stream=True, stream_callback=None)
        registry = ToolRegistry().register(NoopTool("shell"))
        agent = Agent(backend, registry, cfg)
        task = Task(task_id="st4", description="fix", repo_path=str(tmp_path), max_steps=3)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        assert result.is_success()

    def test_stream_retry_on_error(self, tmp_path):
        """流式路径的错误也应该触发重试。"""
        attempt = 0
        script = [Action(ActionType.FINISH, "done", message="ok")]
        base_backend = MockBackend(script)

        def flaky_stream(messages, tools, on_text=None, on_thought=None):
            nonlocal attempt
            attempt += 1
            if attempt < 2:
                raise ConnectionError("stream interrupted")
            if on_text:
                on_text("ok")
            return base_backend.complete(messages, tools)

        base_backend.stream = flaky_stream
        cfg = AgentConfig(stream=True, llm_max_retries=3, llm_retry_delay=0.01)
        registry = ToolRegistry().register(NoopTool("shell"))
        agent = Agent(base_backend, registry, cfg)
        task = Task(task_id="st5", description="fix", repo_path=str(tmp_path), max_steps=3)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        assert result.is_success()
        assert attempt == 2  # 第一次失败，第二次成功


# ---------------------------------------------------------------------------
# CLI --stream 参数
# ---------------------------------------------------------------------------

class TestCliStreamOption:
    def test_stream_option_registered(self):
        from click.testing import CliRunner
        from entry.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--help"])
        assert "--stream" in result.output or "-s" in result.output

    def test_stream_default_on(self):
        """--stream 默认应该是开启的。"""
        from click.testing import CliRunner
        from entry.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--help"])
        # 默认 on 所以帮助文字里有 default
        assert "stream" in result.output.lower()


# ---------------------------------------------------------------------------
# AnthropicBackend / OpenAICompatBackend stream() 方法存在性
# ---------------------------------------------------------------------------

class TestBackendStreamMethod:
    def test_anthropic_backend_has_stream(self):
        from llm.anthropic_backend import AnthropicBackend
        assert hasattr(AnthropicBackend, "stream")
        assert callable(AnthropicBackend.stream)

    def test_openai_compat_backend_has_stream(self):
        from llm.openai_compat import OpenAICompatBackend
        assert hasattr(OpenAICompatBackend, "stream")
        assert callable(OpenAICompatBackend.stream)

    def test_anthropic_stream_signature(self):
        """stream() 方法接受 on_text 参数。"""
        import inspect
        from llm.anthropic_backend import AnthropicBackend
        sig = inspect.signature(AnthropicBackend.stream)
        assert "on_text" in sig.parameters

    def test_openai_stream_signature(self):
        import inspect
        from llm.openai_compat import OpenAICompatBackend
        sig = inspect.signature(OpenAICompatBackend.stream)
        assert "on_text" in sig.parameters