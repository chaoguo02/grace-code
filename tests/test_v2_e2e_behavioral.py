"""
L5: Behavioral E2E tests for V2 delegation.

These tests use a REAL LLM backend (DeepSeek) to verify that the model
actually obeys the system-prompt delegation guidance. Assertions are based
on tool-call presence in the EventLog, not on text content (which is
non-deterministic).

Cost control:
  - max_steps=5, budget_tokens=20_000 per parent session
  - child_max_steps=3, child_budget_tokens=10_000
  - Expected cost: ~$0.01-0.05 per test with DeepSeek

Run:
  python -m pytest tests/test_v2_e2e_behavioral.py -v -m e2e
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv()

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("DEEPSEEK_API_KEY"),
        reason="DEEPSEEK_API_KEY not set, skipping E2E behavioral tests",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_real_runtime(tmp_path: Path, *, max_steps: int = 5):
    """Create a SessionRuntime backed by a real LLM but with NoopTool safety."""
    from agent.core import AgentConfig
    from agent.v2 import AgentRegistryV2, SessionRuntime, SessionStore
    from llm.router import create_backend
    from tools.base import NoopTool, ToolRegistry

    backend = create_backend(
        provider=os.environ.get("FORGE_LLM_PROVIDER", "deepseek"),
        model=os.environ.get("FORGE_LLM_MODEL", "deepseek/deepseek-v4-flash"),
        base_url=os.environ.get("FORGE_LLM_BASE_URL") or None,
        max_tokens=2048,
        timeout_seconds=30.0,
    )

    agent_registry = AgentRegistryV2()
    base_registry = ToolRegistry()
    all_tool_names = {
        tool_name
        for spec in (
            agent_registry.list_primary_agents()
            + agent_registry.list_subagents()
        )
        for tool_name in spec.allowed_tools
    }
    for tool_name in sorted(all_tool_names):
        base_registry.register(NoopTool(tool_name, output=f"[noop] {tool_name} executed successfully"))

    log_dir = str(tmp_path / "logs")
    store = SessionStore(str(tmp_path / ".forge-agent" / "v2" / "sessions.db"))
    runtime = SessionRuntime(
        store=store,
        backend=backend,
        base_registry=base_registry,
        agent_registry=agent_registry,
        root_agent_config=AgentConfig(
            max_steps=max_steps,
            budget_tokens=20_000,
            request_budget_tokens=15_000,
            history_max_messages=20,
            stream=False,
        ),
        log_dir=log_dir,
        child_max_steps=3,
        child_budget_tokens=10_000,
        memory_context=None,
    )
    return runtime, store, log_dir


def _get_tool_calls_from_log(log_dir: str) -> list[dict]:
    """Parse all EventLog JSONL files and extract tool calls from ACTION events."""
    tool_calls = []
    log_path = Path(log_dir)
    if not log_path.exists():
        return tool_calls
    for jsonl_file in log_path.glob("*.jsonl"):
        with open(jsonl_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                if event.get("event_type") != "action":
                    continue
                payload = event.get("payload", {})
                action = payload.get("action", {})
                for tc in action.get("tool_calls", []):
                    tool_calls.append(tc)
    return tool_calls


def _parent_called_task(log_dir: str) -> bool:
    """Return True if any tool call in the log has name == 'task'."""
    return any(tc.get("name") == "task" for tc in _get_tool_calls_from_log(log_dir))


# ---------------------------------------------------------------------------
# Test Scenarios
# ---------------------------------------------------------------------------


class TestE2EBehavioral:
    """L5 behavioral E2E tests: real LLM, noop tools, tool-call assertions."""

    def test_simple_task_no_delegation(self, tmp_path):
        """LLM should NOT delegate a trivial arithmetic question."""
        runtime, store, log_dir = _make_real_runtime(tmp_path)
        root = runtime.create_root_session(
            agent_name="build",
            repo_path=str(tmp_path),
            title="simple task test",
        )
        runtime.run_session(
            root.id,
            agent_name="build",
            task_description="告诉我 1+1 等于多少，直接回答即可",
            intent="analysis",
        )
        assert not _parent_called_task(log_dir), (
            "LLM should not delegate a trivial task"
        )

    def test_complex_task_triggers_delegation(self, tmp_path):
        """LLM should delegate a complex multi-step analysis task."""
        runtime, store, log_dir = _make_real_runtime(tmp_path)
        root = runtime.create_root_session(
            agent_name="build",
            repo_path=str(tmp_path),
            title="complex delegation test",
        )
        runtime.run_session(
            root.id,
            agent_name="build",
            task_description=(
                "请使用子代理(task工具)来完成以下任务：分析 src/auth 目录下所有文件的依赖关系，"
                "找出循环依赖并输出 Mermaid 图。这个任务比较复杂，适合委派给 explore 子代理。"
            ),
            intent="analysis",
        )
        assert _parent_called_task(log_dir), (
            "LLM should delegate a complex analysis task to a subagent"
        )

    def test_negation_respects_no_delegation(self, tmp_path):
        """LLM should NOT delegate when user explicitly forbids it."""
        runtime, store, log_dir = _make_real_runtime(tmp_path)
        root = runtime.create_root_session(
            agent_name="build",
            repo_path=str(tmp_path),
            title="negation test",
        )
        runtime.run_session(
            root.id,
            agent_name="build",
            task_description=(
                "不要使用 task 工具，不要委派子代理，不要开子会话。"
                "直接告诉我 Python 的 list 和 tuple 有什么区别。"
            ),
            intent="analysis",
        )
        assert not _parent_called_task(log_dir), (
            "LLM should respect explicit no-delegation instruction"
        )

    def test_context_passed_to_child_prompt(self, tmp_path):
        """When delegating, LLM should embed parent context into child prompt."""
        runtime, store, log_dir = _make_real_runtime(tmp_path)
        root = runtime.create_root_session(
            agent_name="build",
            repo_path=str(tmp_path),
            title="context passing test",
        )
        runtime.run_session(
            root.id,
            agent_name="build",
            task_description=(
                "你必须使用 task 工具来完成这个任务，调用 task 工具时 subagent_type 填 explore。"
                "任务内容：分析 login.py 文件的认证逻辑。"
                "关键背景：JWT token 过期时间设为 24 小时。"
                "你在调用 task 工具时，必须在 prompt 参数中包含 'JWT' 和 '24' 这两个关键词。"
            ),
            intent="analysis",
        )
        task_calls = [
            tc for tc in _get_tool_calls_from_log(log_dir)
            if tc.get("name") == "task"
        ]
        assert task_calls, "LLM should have delegated via task tool"
        prompt_text = task_calls[0].get("params", {}).get("prompt", "")
        assert "JWT" in prompt_text or "24" in prompt_text, (
            f"Child prompt should contain context about JWT/24h, got: {prompt_text[:200]}"
        )

    def test_partial_child_result_parent_continues(self, tmp_path):
        """Parent LLM should handle a partial child result gracefully."""
        from unittest.mock import patch

        from tools.base import ToolResult

        runtime, store, log_dir = _make_real_runtime(tmp_path, max_steps=6)
        root = runtime.create_root_session(
            agent_name="build",
            repo_path=str(tmp_path),
            title="partial recovery test",
        )

        fake_partial = ToolResult(
            success=True,
            output=(
                "已分析了 3/10 个模块的架构。\n\n"
                "[Note: Child session stopped before fully covering the requested scope.]"
            ),
            error=None,
        )
        with patch(
            "agent.v2.task_tool.TaskToolV2.execute",
            return_value=fake_partial,
        ):
            result = runtime.run_session(
                root.id,
                agent_name="build",
                task_description=(
                    "请用 task 工具委派子代理分析整个项目的 10 个核心模块的架构。"
                    "即使子代理只完成了部分工作，也请根据已有结果给出总结。"
                ),
                intent="analysis",
            )

        assert result.summary, "Parent should produce a summary even with partial child"
        assert len(result.summary) > 10, (
            f"Summary too short, possibly empty: {result.summary!r}"
        )
