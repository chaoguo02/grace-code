"""
tests/test_day4.py

Day 4 测试：LLM Router、两个 backend 的格式解析、prompt 模块。

不调用真实 API——用 unittest.mock 拦截 SDK 调用，
验证请求格式正确、响应解析正确。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.prompt import (
    build_system_prompt,
    build_task_prompt,
    reflection_no_edit,
    reflection_test_failed,
)
from agent.task import ActionType
from llm.base import LLMMessage, LLMToolSchema
from llm.router import create_backend, create_backend_from_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_tool_schema(name="shell", desc="run shell") -> LLMToolSchema:
    return LLMToolSchema(
        name=name,
        description=desc,
        parameters={
            "type": "object",
            "properties": {"cmd": {"type": "string"}},
            "required": ["cmd"],
        },
    )


def make_messages(*pairs) -> list[LLMMessage]:
    """make_messages("system", "...", "user", "...") → [LLMMessage, ...]"""
    msgs = []
    it = iter(pairs)
    for role in it:
        content = next(it)
        msgs.append(LLMMessage(role=role, content=content))
    return msgs


# ===========================================================================
# prompt.py
# ===========================================================================

class TestBuildSystemPrompt:
    def test_contains_repo_path(self):
        prompt = build_system_prompt("/my/repo", [])
        assert "/my/repo" in prompt

    def test_contains_tool_names(self):
        tools = [make_tool_schema("shell"), make_tool_schema("file_read")]
        prompt = build_system_prompt(".", tools)
        assert "shell" in prompt
        assert "file_read" in prompt

    def test_no_tools_placeholder(self):
        prompt = build_system_prompt(".", [])
        assert "no tools" in prompt.lower()

    def test_repo_summary_injected(self):
        prompt = build_system_prompt(".", [], repo_summary="MyProject: 42 files")
        assert "MyProject: 42 files" in prompt

    def test_no_summary_fallback(self):
        prompt = build_system_prompt(".", [])
        assert "Repository summary not yet available" in prompt


class TestBuildTaskPrompt:
    def test_contains_description(self):
        prompt = build_task_prompt("Fix the parser bug", "/repo")
        assert "Fix the parser bug" in prompt

    def test_contains_repo_path(self):
        prompt = build_task_prompt("Do something", "/my/repo")
        assert "/my/repo" in prompt

    def test_with_issue_url(self):
        prompt = build_task_prompt("Fix X", "/repo", issue_url="https://github.com/org/repo/issues/42")
        assert "https://github.com/org/repo/issues/42" in prompt

    def test_without_issue_url(self):
        prompt = build_task_prompt("Fix X", "/repo")
        assert "github.com" not in prompt


class TestReflectionPrompts:
    def test_test_failed_prompt(self):
        p = reflection_test_failed()
        assert "REFLECTION" in p
        assert "root cause" in p.lower() or "error" in p.lower()

    def test_no_edit_prompt_contains_n(self):
        p = reflection_no_edit(7)
        assert "7" in p

    def test_no_edit_prompt_reflection_marker(self):
        p = reflection_no_edit(3)
        assert "REFLECTION" in p


# ===========================================================================
# llm/router.py
# ===========================================================================

class TestRouter:
    def test_anthropic_provider(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        with patch("llm.anthropic_backend.AnthropicBackend.__init__", return_value=None):
            backend = create_backend("anthropic", "claude-sonnet-4-5", api_key="sk-ant-test")
        from llm.anthropic_backend import AnthropicBackend
        assert isinstance(backend, AnthropicBackend)

    def test_deepseek_provider(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-test")
        with patch("llm.openai_backend.OpenAIBackend.__init__", return_value=None):
            backend = create_backend("deepseek", "deepseek-chat", api_key="sk-ds-test")
        from llm.openai_backend import OpenAIBackend
        assert isinstance(backend, OpenAIBackend)

    def test_openai_provider(self):
        with patch("llm.openai_backend.OpenAIBackend.__init__", return_value=None):
            backend = create_backend("openai", "gpt-4o", api_key="sk-oai-test")
        from llm.openai_backend import OpenAIBackend
        assert isinstance(backend, OpenAIBackend)

    def test_groq_provider(self):
        with patch("llm.openai_backend.OpenAIBackend.__init__", return_value=None):
            backend = create_backend("groq", "llama3-70b-8192", api_key="sk-groq-test")
        from llm.openai_backend import OpenAIBackend
        assert isinstance(backend, OpenAIBackend)

    def test_ollama_no_key_required(self):
        with patch("llm.openai_backend.OpenAIBackend.__init__", return_value=None):
            # ollama 不需要 api_key
            backend = create_backend("ollama", "llama3", api_key=None)
        from llm.openai_backend import OpenAIBackend
        assert isinstance(backend, OpenAIBackend)

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unsupported provider"):
            create_backend("unknown_llm", "some-model", api_key="x")

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValueError, match="API key"):
            create_backend("anthropic", "claude-sonnet-4-5", api_key=None)

    def test_from_config_dict(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        with patch("llm.anthropic_backend.AnthropicBackend.__init__", return_value=None):
            backend = create_backend_from_config({
                "provider": "anthropic",
                "model": "claude-sonnet-4-5",
                "api_key": "sk-ant-test",
            })
        from llm.anthropic_backend import AnthropicBackend
        assert isinstance(backend, AnthropicBackend)

    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-key")
        with patch("llm.openai_backend.OpenAIBackend.__init__", return_value=None):
            # 不传 api_key，从环境变量读
            backend = create_backend("deepseek", "deepseek-chat")
        from llm.openai_backend import OpenAIBackend
        assert isinstance(backend, OpenAIBackend)


# ===========================================================================
# AnthropicBackend — 格式解析（mock SDK）
# ===========================================================================

class TestAnthropicBackend:

    def _make_response(self, stop_reason, content_blocks):
        """构造一个 mock Anthropic API 响应。"""
        response = MagicMock()
        response.stop_reason = stop_reason
        response.content = content_blocks
        response.usage.input_tokens = 100
        response.usage.output_tokens = 50
        return response

    def _make_text_block(self, text):
        block = MagicMock()
        block.type = "text"
        block.text = text
        return block

    def _make_tool_use_block(self, name, input_data):
        block = MagicMock()
        block.type = "tool_use"
        block.id = "toolu_test123"
        block.name = name
        block.input = input_data
        return block

    def _make_backend(self):
        with patch("anthropic.Anthropic"):
            from llm.anthropic_backend import AnthropicBackend
            backend = AnthropicBackend(model="claude-sonnet-4-5", api_key="sk-test")
            return backend

    def test_tool_use_response_parsed(self):
        backend = self._make_backend()
        response = self._make_response("tool_use", [
            self._make_text_block("I will run the tests."),
            self._make_tool_use_block("shell", {"cmd": "pytest tests/"}),
        ])
        backend._client.messages.create.return_value = response

        result = backend.complete(
            make_messages("system", "you are an agent", "user", "fix it"),
            [make_tool_schema()],
        )

        assert result.action.action_type == ActionType.TOOL_CALL
        assert result.action.tool_call.name == "shell"
        assert result.action.tool_call.params == {"cmd": "pytest tests/"}
        assert "I will run" in result.action.thought

    def test_end_turn_response_is_finish(self):
        backend = self._make_backend()
        response = self._make_response("end_turn", [
            self._make_text_block("The task is complete. I fixed the bug."),
        ])
        backend._client.messages.create.return_value = response

        result = backend.complete(make_messages("user", "fix it"), [])
        assert result.action.action_type == ActionType.FINISH

    def test_max_tokens_is_give_up(self):
        backend = self._make_backend()
        response = self._make_response("max_tokens", [
            self._make_text_block("..."),
        ])
        backend._client.messages.create.return_value = response

        result = backend.complete(make_messages("user", "fix it"), [])
        assert result.action.action_type == ActionType.GIVE_UP

    def test_system_message_sent_separately(self):
        backend = self._make_backend()
        response = self._make_response("end_turn", [self._make_text_block("done")])
        backend._client.messages.create.return_value = response

        messages = make_messages("system", "you are helpful", "user", "fix it")
        backend.complete(messages, [])

        call_kwargs = backend._client.messages.create.call_args[1]
        assert call_kwargs["system"] == "you are helpful"
        # messages 里不含 system
        assert all(m["role"] != "system" for m in call_kwargs["messages"])

    def test_token_counts_returned(self):
        backend = self._make_backend()
        response = self._make_response("end_turn", [self._make_text_block("done")])
        backend._client.messages.create.return_value = response

        result = backend.complete(make_messages("user", "fix it"), [])
        assert result.input_tokens == 100
        assert result.output_tokens == 50
        assert result.total_tokens == 150

    def test_tools_converted_to_anthropic_format(self):
        backend = self._make_backend()
        response = self._make_response("end_turn", [self._make_text_block("done")])
        backend._client.messages.create.return_value = response

        backend.complete(make_messages("user", "go"), [make_tool_schema("shell")])

        call_kwargs = backend._client.messages.create.call_args[1]
        tools = call_kwargs["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "shell"
        assert "input_schema" in tools[0]   # Anthropic 格式用 input_schema


# ===========================================================================
# OpenAIBackend — 格式解析（mock SDK）
# ===========================================================================

class TestOpenAIBackend:

    def _make_response(self, finish_reason, content=None, tool_calls=None):
        usage = SimpleNamespace(prompt_tokens=80, completion_tokens=40)
        message = SimpleNamespace(
            content=content,
            tool_calls=tool_calls,
        )
        choice = SimpleNamespace(finish_reason=finish_reason, message=message)
        return SimpleNamespace(choices=[choice], usage=usage)

    def _make_tool_call(self, name, args_dict):
        fn = SimpleNamespace(name=name, arguments=json.dumps(args_dict))
        return SimpleNamespace(id="call_test123", function=fn)

    def _make_backend(self, model="gpt-4o"):
        with patch("openai.OpenAI"):
            from llm.openai_backend import OpenAIBackend
            backend = OpenAIBackend(model=model, api_key="sk-test")
            return backend

    def test_tool_call_response_parsed(self):
        backend = self._make_backend()
        response = self._make_response(
            "tool_calls",
            content="Let me run the tests.",
            tool_calls=[self._make_tool_call("shell", {"cmd": "pytest"})],
        )
        backend._client.chat.completions.create.return_value = response

        result = backend.complete(make_messages("user", "fix it"), [make_tool_schema()])
        assert result.action.action_type == ActionType.TOOL_CALL
        assert result.action.tool_call.name == "shell"
        assert result.action.tool_call.params == {"cmd": "pytest"}

    def test_stop_response_is_finish(self):
        backend = self._make_backend()
        response = self._make_response("stop", content="Task is done.")
        backend._client.chat.completions.create.return_value = response

        result = backend.complete(make_messages("user", "fix it"), [])
        assert result.action.action_type == ActionType.FINISH

    def test_length_finish_reason_is_give_up(self):
        backend = self._make_backend()
        response = self._make_response("length", content="...")
        backend._client.chat.completions.create.return_value = response

        result = backend.complete(make_messages("user", "fix it"), [])
        assert result.action.action_type == ActionType.GIVE_UP

    def test_tools_converted_to_openai_format(self):
        backend = self._make_backend()
        response = self._make_response("stop", content="done")
        backend._client.chat.completions.create.return_value = response

        backend.complete(make_messages("user", "go"), [make_tool_schema("shell")])

        call_kwargs = backend._client.chat.completions.create.call_args[1]
        tools = call_kwargs["tools"]
        assert tools[0]["type"] == "function"
        assert tools[0]["function"]["name"] == "shell"

    def test_token_counts_returned(self):
        backend = self._make_backend()
        response = self._make_response("stop", content="done")
        backend._client.chat.completions.create.return_value = response

        result = backend.complete(make_messages("user", "fix it"), [])
        assert result.input_tokens == 80
        assert result.output_tokens == 40


# ===========================================================================
# OpenAIBackend — 文本解析 fallback（R1 模型）
# ===========================================================================

class TestTextFallback:

    def _make_backend(self):
        with patch("openai.OpenAI"):
            from llm.openai_backend import OpenAIBackend
            # deepseek-reasoner 不支持 function calling
            backend = OpenAIBackend(model="deepseek-reasoner", api_key="sk-test")
            return backend

    def _make_response(self, text):
        usage = SimpleNamespace(prompt_tokens=50, completion_tokens=30)
        message = SimpleNamespace(content=text, tool_calls=None)
        choice = SimpleNamespace(finish_reason="stop", message=message)
        return SimpleNamespace(choices=[choice], usage=usage)

    def test_r1_model_no_function_calling(self):
        from llm.openai_backend import OpenAIBackend
        with patch("openai.OpenAI"):
            backend = OpenAIBackend(model="deepseek-reasoner", api_key="sk-test")
        assert not backend.supports_function_calling

    def test_json_block_parsed_as_tool_call(self):
        backend = self._make_backend()
        text = '```json\n{"tool": "shell", "params": {"cmd": "pytest"}}\n```'
        backend._client.chat.completions.create.return_value = self._make_response(text)

        result = backend.complete(make_messages("user", "fix it"), [make_tool_schema()])
        assert result.action.action_type == ActionType.TOOL_CALL
        assert result.action.tool_call.name == "shell"

    def test_task_complete_keyword(self):
        backend = self._make_backend()
        backend._client.chat.completions.create.return_value = self._make_response(
            "TASK_COMPLETE: Fixed the parser bug by correcting the regex."
        )
        result = backend.complete(make_messages("user", "fix it"), [])
        assert result.action.action_type == ActionType.FINISH
        assert "Fixed the parser" in result.action.message

    def test_give_up_keyword(self):
        backend = self._make_backend()
        backend._client.chat.completions.create.return_value = self._make_response(
            "GIVE_UP: The issue requires access to external systems."
        )
        result = backend.complete(make_messages("user", "fix it"), [])
        assert result.action.action_type == ActionType.GIVE_UP

    def test_unparseable_text_is_give_up(self):
        backend = self._make_backend()
        backend._client.chat.completions.create.return_value = self._make_response(
            "I am thinking about the problem... hmm..."
        )
        result = backend.complete(make_messages("user", "fix it"), [])
        assert result.action.action_type == ActionType.GIVE_UP

    def test_tool_description_injected_in_system(self):
        """R1 模式下，工具描述应注入 system prompt 而非 tools 参数。"""
        backend = self._make_backend()
        backend._client.chat.completions.create.return_value = self._make_response(
            "TASK_COMPLETE: done"
        )
        backend.complete(
            make_messages("system", "you are an agent", "user", "fix it"),
            [make_tool_schema("shell")],
        )
        call_kwargs = backend._client.chat.completions.create.call_args[1]
        # tools 参数不应该存在
        assert "tools" not in call_kwargs
        # system 消息内容应包含工具描述
        system_content = call_kwargs["messages"][0]["content"]
        assert "shell" in system_content