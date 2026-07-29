from __future__ import annotations

from types import SimpleNamespace

from agent.core import ReActAgent
from agent.task import ActionType
from llm.base import StreamEvent, StreamEventKind


class _Backend:
    model_name = "routing-test"

    def stream_iter(self, messages, tools):
        yield StreamEvent(
            kind=StreamEventKind.TEXT_DELTA,
            text="internal reasoning",
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
