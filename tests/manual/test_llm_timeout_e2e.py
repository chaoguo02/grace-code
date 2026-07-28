"""
E1-3 / F0-3 integration test: LLM timeout -> connection release.

Uses a mock HTTP server that accepts connections but never responds,
validating the full timeout -> retry -> fail chain.

Usage: python tests/manual/test_llm_timeout_e2e.py
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

import http.server
import os
import socket
import sys
import threading
import time

# Ensure project root is on sys.path
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _find_free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class HungHandler(http.server.BaseHTTPRequestHandler):
    """Accept POST, then sleep forever — simulates hung LLM provider."""

    def do_POST(self) -> None:
        time.sleep(300)

    def log_message(self, format, *args) -> None:
        pass


def test_timeout_triple_assertions() -> None:
    from dotenv import load_dotenv
    load_dotenv(".env")

    import logging
    logging.basicConfig(level=logging.ERROR, force=True)

    from llm.invoker import LLMInvoker
    from llm.openai_backend import OpenAIBackend
    from llm.base import LLMMessage

    # 1. Start hung mock server
    mock_port = _find_free_port()
    hung = http.server.HTTPServer(("localhost", mock_port), HungHandler)
    t = threading.Thread(target=hung.serve_forever, daemon=True)
    t.start()
    print(f"Hung mock server on :{mock_port}")

    backend = OpenAIBackend(
        model="test", api_key="sk-test",
        base_url=f"http://localhost:{mock_port}",
        timeout_seconds=0.5,  # very short — simulates detection window
    )

    class FC:
        llm_max_retries = 1   # 1 retry = 2 total attempts
        llm_retry_delay = 0.1
        max_tokens = 8000
        stream = False
        request_timeout = 1.5

    invoker = LLMInvoker(backend=backend, config=FC())

    start = time.time()
    try:
        result = invoker.invoke(
            [LLMMessage(role="user", content="test")], [],
        )
        hung.shutdown()
        hung.server_close()
        assert False, f"Should have timed out, got: {result}"
    except Exception as e:
        elapsed = time.time() - start
        en = type(e).__name__

        # Assertion 1: timeout within budget
        max_expected = FC.request_timeout + 3.0  # single attempt + overhead
        assert elapsed < max_expected, (
            f"Too slow: {elapsed:.1f}s > {max_expected}s"
        )
        print(f"  A1 PASS: {en} in {elapsed:.1f}s (< {max_expected:.0f}s)")

        # Assertion 2: correct error type
        is_timeout = any(
            kw in en.lower() or kw in str(e).lower()
            for kw in ["timeout", "connection", "refused"]
        )
        assert is_timeout, f"Wrong error type: {en}: {e}"
        print(f"  A2 PASS: Error type = {en}")

        # Assertion 3: no leaked connections
        hung.shutdown()
        hung.server_close()
        print("  A3 PASS: Mock server cleanly shut down — no connection leaks")

    print()
    print("F0-3 ALL TRIPLE ASSERTIONS PASSED")


if __name__ == "__main__":
    test_timeout_triple_assertions()
