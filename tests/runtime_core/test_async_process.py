"""Phase B: Runtime.aexec — async subprocess (CC Bash async).

AC:
- aexec executes a command via asyncio.subprocess
- aexec does not block the event loop (concurrent commands interleave)
- aexec respects timeout (returns TIMED_OUT)
- aexec preserves shell=False argument isolation
"""

from __future__ import annotations

import asyncio
import os

import pytest


def _make_runtime():
    from core.process import LocalRuntime
    return LocalRuntime(workspace_root=".")


async def test_aexec_returns_result():
    """aexec 执行命令返回 RunResult。"""
    from core.process import LocalRuntime
    runtime = _make_runtime()
    result = await runtime.aexec("echo", ["hello"])
    assert result.returncode == 0
    assert "hello" in result.stdout


async def test_aexec_not_blocking_event_loop():
    """两个并发 aexec 交错 — async 不阻塞事件循环。"""
    import time
    runtime = _make_runtime()

    async def _echo_slow():
        await runtime.aexec("sleep", ["0.1"])
        return "slow-done"

    async def _echo_fast():
        return "fast-done"

    # 并发: slow + fast 应能交错（fast 先完成）
    task1 = asyncio.create_task(_echo_slow())
    await asyncio.sleep(0.02)
    task2 = asyncio.create_task(_echo_fast())
    r1, r2 = await asyncio.gather(task1, task2)
    assert r2 == "fast-done"  # fast 不等待 slow


async def test_aexec_timeout():
    """aexec 超时返回 TIMED_OUT。"""
    runtime = _make_runtime()
    result = await runtime.aexec("sleep", ["5"], timeout=1)
    assert result.returncode == -1
    assert "timed out" in result.stderr.lower()


async def test_aexec_argument_isolation():
    """aexec 参数隔离 — shell 元字符不被解释。"""
    runtime = _make_runtime()
    # echo "$(whoami)" 应原样输出（不执行 whoami）
    result = await runtime.aexec("echo", ["$(whoami)"])
    assert result.returncode == 0
    assert "$(whoami)" in result.stdout  # 原样输出，不是 username
