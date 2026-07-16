"""
Memory safety checks adapted to the current forge-agent APIs.

Run from repo root:
    python _memory_safety_tests.py
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from memory.consolidation import (
    _acquire_lock,
    _release_lock,
    _validate_memory_dir,
    run_consolidation,
)
from memory.dream_agent import DreamAgent


class DummyStore:
    def __init__(self, store_dir: Path) -> None:
        self.store_dir = store_dir


class NoopBackend:
    def complete(self, messages, tools=None):
        class Response:
            text = json.dumps({"summary": "noop", "tool_calls": []})
        return Response()


def check_session_stop_non_blocking() -> None:
    with tempfile.TemporaryDirectory(prefix="memory_async_") as d:
        store = DummyStore(Path(d))
        start = time.perf_counter()
        result = run_consolidation(
            store,
            sessions_since_last_dream=5,
            backend=NoopBackend(),
            async_run=True,
        )
        latency_ms = (time.perf_counter() - start) * 1000
        assert result is True
        assert latency_ms < 50, f"async consolidation submission too slow: {latency_ms:.2f}ms"
        print(f"OK async Dream submission is non-blocking: {latency_ms:.2f}ms")


def check_lock_competition() -> None:
    with tempfile.TemporaryDirectory(prefix="memory_lock_") as d:
        memory_dir = Path(d)
        lock_path = memory_dir / ".consolidate-lock"
        store = DummyStore(memory_dir)
        assert _acquire_lock(lock_path)
        try:
            result = run_consolidation(
                store,
                sessions_since_last_dream=5,
                backend=NoopBackend(),
                async_run=True,
            )
            assert result is False, "lock contention should skip consolidation"
        finally:
            _release_lock(lock_path)
        assert lock_path.exists()
        assert lock_path.read_text(encoding="utf-8") == ""
        print("OK lock contention skips async Dream safely")


def check_stale_memory_warning() -> None:
    with tempfile.TemporaryDirectory(prefix="memory_stale_") as d:
        memory_dir = Path(d)
        stale = memory_dir / "stale.md"
        stale.write_text("old", encoding="utf-8")
        old_time = time.time() - int(86400 * 2.1)
        stale.touch()
        import os
        os.utime(stale, (old_time, old_time))

        fresh = memory_dir / "fresh.md"
        fresh.write_text("new", encoding="utf-8")

        agent = DreamAgent(memory_dir=memory_dir, backend=NoopBackend())
        stale_warning = agent._memory_freshness_text(stale)
        fresh_warning = agent._memory_freshness_text(fresh)
        assert "point-in-time" in stale_warning
        assert fresh_warning == ""
        prompt = agent._build_messages()[1].content
        assert "stale.md" in prompt and "point-in-time" in prompt
        assert "fresh.md" in prompt
        print("OK stale memory warning is injected for old files only")


def check_sensitive_path_rejection() -> None:
    legal = _validate_memory_dir(Path("memory/user_data"))
    assert legal.name == "user_data"

    sensitive = [Path.home() / ".ssh", Path.home() / ".aws", Path("memory/../.config")]
    for path in sensitive:
        try:
            _validate_memory_dir(path)
        except ValueError:
            pass
        else:
            raise AssertionError(f"sensitive path was not rejected: {path}")

    nested = _validate_memory_dir(Path("memory/.config/app"))
    assert nested.name == "app"
    print("OK sensitive memory roots are rejected while safe nested dirs pass")


def main() -> None:
    check_session_stop_non_blocking()
    check_lock_competition()
    check_stale_memory_warning()
    check_sensitive_path_rejection()
    print("ALL MEMORY SAFETY CHECKS PASSED")


if __name__ == "__main__":
    main()
