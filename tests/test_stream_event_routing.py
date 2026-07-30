from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import llm.openai_backend as openai_backend
from agent.core import ReActAgent
from agent.task import Action, ActionType
from llm.base import LLMBackend, LLMResponse, StreamEvent, StreamEventKind


class _Backend:
    model_name = "routing-test"

    def stream_iter(self, messages, tools):
        yield StreamEvent(
            kind=StreamEventKind.TEXT_DELTA,
            thought="internal reasoning",
        )
        yield StreamEvent(
            kind=StreamEventKind.TEXT_DELTA,
            text="visible answer",
        )
        yield StreamEvent(
            kind=StreamEventKind.FINISH,
            finish_message="visible answer",
        )


class _Executor:
    def process_queue(self):
        return None

    def enqueue(self, _call):
        return None


class _FallbackBackend(LLMBackend):
    @property
    def model_name(self):
        return "fallback-test"

    def complete(self, messages, tools):
        return LLMResponse(
            action=Action(
                ActionType.FINISH,
                thought="visible answer",
                message="visible answer",
            ),
            raw_content="visible answer",
        )


def test_stream_routes_reasoning_and_visible_text_to_distinct_channels():
    thoughts: list[str] = []
    text_deltas: list[str] = []
    lifecycle: list[str] = []

    agent = object.__new__(ReActAgent)
    agent._backend = _Backend()
    agent._registry = SimpleNamespace(skill_runtime_overrides={})
    agent._cfg = SimpleNamespace(
        stream_callback=thoughts.append,
        text_stream_lifecycle_callback=(
            lambda event, block_id, *rest: lifecycle.append(event)
        ),
        text_stream_delta_callback=(
            lambda block_id, text: text_deltas.append(text)
        ),
        request_timeout=1.0,
        cancellation_token=None,
    )

    action = agent._stream_and_dispatch([], [], _Executor())

    assert action.action_type is ActionType.FINISH
    assert action.thought == "internal reasoning"
    assert action.message == "visible answer"
    assert thoughts == ["internal reasoning"]
    assert text_deltas == ["visible answer"]
    assert lifecycle == ["start", "end"]


def test_plain_text_response_is_not_mirrored_into_thought():
    action = openai_backend._parse_text_response("visible answer")

    assert action.action_type is ActionType.FINISH
    assert action.thought == ""
    assert action.message == "visible answer"


def test_base_stream_iter_normalizes_legacy_mirrored_answer():
    events = list(_FallbackBackend().stream_iter([], []))

    assert [
        (event.kind, event.text, event.thought, event.finish_message)
        for event in events
    ] == [
        (StreamEventKind.TEXT_DELTA, "visible answer", "", ""),
        (StreamEventKind.FINISH, "visible answer", "", "visible answer"),
    ]


def test_openai_stream_iter_uses_only_reasoning_callback_as_thought(monkeypatch):
    def fake_stream(self, messages, tools, on_text=None, on_thought=None):
        on_text("visible answer")
        return LLMResponse(
            action=Action(
                ActionType.FINISH,
                thought="visible answer",
                message="visible answer",
            ),
            raw_content="visible answer",
        )

    monkeypatch.setattr(openai_backend, "_openai_stream", fake_stream)

    events = list(openai_backend._openai_stream_iter(None, [], []))

    assert [
        (event.kind, event.text, event.thought, event.finish_message)
        for event in events
    ] == [
        (StreamEventKind.TEXT_DELTA, "visible answer", "", ""),
        (StreamEventKind.FINISH, "visible answer", "", "visible answer"),
    ]


def test_openai_stream_iter_keeps_reasoning_separate_from_answer(monkeypatch):
    def fake_stream(self, messages, tools, on_text=None, on_thought=None):
        on_thought("internal reasoning")
        on_text("visible answer")
        return LLMResponse(
            action=Action(
                ActionType.FINISH,
                thought="internal reasoning",
                message="visible answer",
            ),
            raw_content="visible answer",
        )

    monkeypatch.setattr(openai_backend, "_openai_stream", fake_stream)

    events = list(openai_backend._openai_stream_iter(None, [], []))

    assert [
        (event.kind, event.text, event.thought, event.finish_message)
        for event in events
    ] == [
        (StreamEventKind.TEXT_DELTA, "", "internal reasoning", ""),
        (StreamEventKind.TEXT_DELTA, "visible answer", "", ""),
        (
            StreamEventKind.FINISH,
            "visible answer",
            "internal reasoning",
            "visible answer",
        ),
    ]


def test_openai_stream_iter_yields_before_provider_stream_finishes(monkeypatch):
    release_provider = threading.Event()

    def fake_stream(self, messages, tools, on_text=None, on_thought=None):
        on_text("first")
        release_provider.wait(timeout=1)
        return LLMResponse(
            action=Action(
                ActionType.FINISH,
                thought="",
                message="first second",
            ),
            raw_content="first second",
        )

    monkeypatch.setattr(openai_backend, "_openai_stream", fake_stream)
    events = openai_backend._openai_stream_iter(None, [], [])

    started = time.monotonic()
    first = next(events)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert (first.kind, first.text, first.thought) == (
        StreamEventKind.TEXT_DELTA,
        "first",
        "",
    )

    release_provider.set()
    remaining = list(events)
    assert remaining[-1].kind is StreamEventKind.FINISH
