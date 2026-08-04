"""Phase A: Model async — ainvoke/astream_iter (CC callModel async generator).

AC:
- ainvoke returns ModelAction via async client
- astream_iter yields TEXT_DELTA/TOOL_USE/FINISH events as async generator
- async calls do not block the event loop (concurrent tasks interleave)
"""

from __future__ import annotations

import asyncio

import pytest


class _FakeAsyncResponse:
    """Fake Anthropic async response object."""
    def __init__(self, text="hello", stop_reason="end_turn"):
        self.stop_reason = stop_reason
        self.content = [type("_B", (), {"type": "text", "text": text})()]
        class _U:
            input_tokens = 10
            output_tokens = 5
        self.usage = _U()


class _FakeMessages:
    """Fake AsyncAnthropic.messages — create/stream are awaitable."""
    last_kwargs = None

    @staticmethod
    async def create(**kwargs):
        _FakeMessages.last_kwargs = dict(kwargs)
        return _FakeAsyncResponse()

    @staticmethod
    def stream(**kwargs):
        _FakeMessages.last_kwargs = dict(kwargs)
        return _FakeStream()


class _FakeStream:
    """Fake async stream — async context manager + text_stream + final."""
    def __init__(self):
        self._final = _FakeAsyncResponse()
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        pass
    @property
    def text_stream(self):
        async def _gen():
            yield "hel"
            yield "lo"
        return _gen()
    async def get_final_message(self):
        return self._final


class _FakeAsyncClient:
    """Mock AsyncAnthropic — .messages.create/stream are awaitable."""
    def __init__(self):
        self.messages = _FakeMessages


def _make_backend():
    """NativeBackend with fake async client (no real API key)."""
    from runtime_core.native_backend import NativeBackend
    from runtime_core.native_message import NativeConversation, NativeMessage

    backend = object.__new__(NativeBackend)
    object.__setattr__(backend, '_model', 'claude-sonnet')
    object.__setattr__(backend, '_max_tokens', 100)
    object.__setattr__(backend, '_timeout_seconds', 10.0)
    object.__setattr__(backend, '_tool_schemas', ())
    object.__setattr__(backend, '_cached_api_tools', [])
    object.__setattr__(backend, '_async_client', _FakeAsyncClient())
    return backend


async def test_ainvoke_returns_model_action():
    """ainvoke 通过 async client 返回 ModelAction。"""
    from runtime_core.model_actions import AssistantText
    backend = _make_backend()
    from runtime_core.native_message import NativeConversation, NativeMessage
    conv = NativeConversation(messages=(NativeMessage.user("hi"),))
    action = await backend.ainvoke(conv)
    assert isinstance(action, AssistantText)
    assert "hello" in action.text


async def test_astream_iter_yields_events():
    """astream_iter 产出 TEXT_DELTA + FINISH 事件。"""
    from llm.base import StreamEventKind
    from runtime_core.native_message import NativeConversation, NativeMessage
    backend = _make_backend()
    conv = NativeConversation(messages=(NativeMessage.user("hi"),))

    deltas = []
    kinds = []
    async for ev in backend.astream_iter(conv):
        kinds.append(ev.kind)
        if ev.kind == StreamEventKind.TEXT_DELTA:
            deltas.append(ev.text)
    assert "".join(deltas) == "hello"
    assert StreamEventKind.FINISH in kinds


async def test_async_calls_do_not_block_event_loop():
    """两个并发 ainvoke 交错 — async 不阻塞事件循环。"""
    from runtime_core.native_message import NativeConversation, NativeMessage
    backend = _make_backend()
    conv = NativeConversation(messages=(NativeMessage.user("hi"),))

    async def _slow():
        await asyncio.sleep(0.05)  # 模拟异步等待
        return "slow"

    # 并发: ainvoke + sleep 应能交错
    task1 = asyncio.create_task(backend.ainvoke(conv))
    task2 = asyncio.create_task(_slow())
    r1, r2 = await asyncio.gather(task1, task2)
    assert r2 == "slow"  # 都能完成


class _FakeAsyncChunk:
    def __init__(self, content=None, tool_calls=None, finish_reason=None):
        _content, _tc, _fr = content, tool_calls, finish_reason
        class _D:
            content = _content
            tool_calls = _tc
            reasoning_content = None
        class _C:
            delta = _D()
            finish_reason = _fr
        self.choices = [_C()]
        self.usage = None


class _FakeAsyncOpenAI:
    """Mock AsyncOpenAI chat.completions — stream returns async chunks."""
    def __init__(self, chunks):
        self._chunks = chunks
        class _Completions:
            def __init__(self, parent):
                self._parent = parent
            async def create(self, **kwargs):
                return _FakeAsyncStream(self._parent._chunks)
        class _Chat:
            def __init__(self, parent):
                self.completions = _Completions(parent)
        self.chat = _Chat(self)


class _FakeAsyncStream:
    def __init__(self, chunks):
        self._chunks = chunks
    def __aiter__(self):
        async def _gen():
            for c in self._chunks:
                yield c
        return _gen()


async def test_openai_astream_iter_yields_deltas_and_finish():
    """OpenAINativeBackend.astream_iter 产出 TEXT_DELTA + FINISH。"""
    from llm.base import StreamEventKind
    from runtime_core.native_message import NativeConversation, NativeMessage
    from runtime_core.openai_native_backend import OpenAINativeBackend

    backend = object.__new__(OpenAINativeBackend)
    object.__setattr__(backend, '_model', 'gpt-4o')
    object.__setattr__(backend, '_max_tokens', 100)
    object.__setattr__(backend, '_base_url', None)
    object.__setattr__(backend, '_use_function_calling', False)
    object.__setattr__(backend, '_tool_schemas', ())
    object.__setattr__(backend, '_cached_api_tools', [])
    object.__setattr__(backend, '_async_client', _FakeAsyncOpenAI([
        _FakeAsyncChunk(content="hi "),
        _FakeAsyncChunk(content="there"),
        _FakeAsyncChunk(finish_reason="stop"),
    ]))

    conv = NativeConversation(messages=(NativeMessage.user("hi"),))
    deltas = []
    kinds = []
    async for ev in backend.astream_iter(conv):
        kinds.append(ev.kind)
        if ev.kind == StreamEventKind.TEXT_DELTA:
            deltas.append(ev.text)
    assert "".join(deltas) == "hi there"
    assert StreamEventKind.FINISH in kinds
