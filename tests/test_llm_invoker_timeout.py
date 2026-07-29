from __future__ import annotations

import threading
import time

import pytest

from agent.session.run_context import CancellationToken
from llm.invoker import LLMInvoker, iter_with_timeout
from llm.openai_backend import OpenAIBackend


class _HungStreamingBackend:
    model_name = "hung-stream"

    def stream(self, messages, tools, *, on_text=None, on_thought=None):
        time.sleep(1)
        raise AssertionError("the abandoned provider thread should not complete")


class _Config:
    llm_max_retries = 1
    llm_retry_delay = 0
    max_tokens = 100
    stream = True
    stream_callback = None
    thought_callback = None
    request_timeout = 0.05
    cancellation_token = None


def test_streaming_provider_attempt_obeys_hard_timeout() -> None:
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="timed out"):
        LLMInvoker(_HungStreamingBackend(), _Config()).invoke([], [])

    assert time.monotonic() - started < 0.5


def test_streaming_provider_wait_is_cancellable_without_retry() -> None:
    token = CancellationToken()
    config = _Config()
    config.request_timeout = 5
    config.cancellation_token = token
    timer = threading.Timer(0.02, token.cancel)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(InterruptedError, match="cancelled"):
            LLMInvoker(_HungStreamingBackend(), config).invoke([], [])
    finally:
        timer.cancel()

    assert time.monotonic() - started < 0.5


def test_stream_iterator_path_obeys_same_deadline() -> None:
    def _hung_iterator():
        time.sleep(1)
        yield "too late"

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="stream timed out"):
        list(iter_with_timeout(_hung_iterator, timeout=0.05))

    assert time.monotonic() - started < 0.5


def test_openai_backend_binds_its_stream_iterator_override() -> None:
    assert OpenAIBackend.stream_iter.__name__ == "_openai_stream_iter"
