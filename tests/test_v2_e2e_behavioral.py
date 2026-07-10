"""
L5: Behavioral E2E tests for V2 fork delegation.

These tests use a REAL LLM backend (DeepSeek) to verify that the model
actually obeys the system-prompt delegation guidance. Assertions are based
on tool-call presence in the EventLog, not on text content.

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
        reason="DEEPSEEK_API_KEY not set",
    ),
]


def _make_real_runtime(tmp_path: Path, *, max_steps: int = 5):
    from agent.core import AgentConfig
    from agent.v2 import AgentRegistryV2, SessionRuntime, SessionStore
    from agent.v2.agent_registry import _BUILD_ALLOWED
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
    for tool_name in sorted(_BUILD_ALLOWED):
        base_registry.register(NoopTool(tool_name, output=f"[noop] {tool_name} executed successfully"))

    log_dir = str(tmp_path / "logs")
    store = SessionStore(str(tmp_path / ".forge-agent" / "v2" / "sessions.db"))
    runtime = SessionRuntime(
        store=store, backend=backend, base_registry=base_registry,
        agent_registry=agent_registry,
        root_agent_config=AgentConfig(
            max_steps=max_steps, budget_tokens=20_000, request_budget_tokens=15_000,
            history_max_messages=20, stream=False,
        ),
        log_dir=log_dir, memory_context=None,
    )
    return runtime, store, log_dir


def _get_tool_calls_from_log(log_dir: str) -> list[dict]:
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
    return any(tc.get("name") == "task" for tc in _get_tool_calls_from_log(log_dir))


class TestE2EBehavioral:
    """L5 behavioral E2E tests: real LLM, noop tools, tool-call assertions."""

    def test_simple_task_no_delegation(self, tmp_path):
        runtime, store, log_dir = _make_real_runtime(tmp_path)
        root = runtime.create_root_session(agent_name="build", repo_path=str(tmp_path), title="simple")
        runtime.run_session(root.id, agent_name="build",
                            task_description="tell me 1+1, answer directly", intent="analysis")
        assert not _parent_called_task(log_dir), "LLM should not delegate a trivial task"

    def test_complex_task_triggers_delegation(self, tmp_path):
        runtime, store, log_dir = _make_real_runtime(tmp_path)
        root = runtime.create_root_session(agent_name="build", repo_path=str(tmp_path), title="complex")
        runtime.run_session(root.id, agent_name="build",
                            task_description=(
                                "You MUST use the task tool with subagent_type='explore' to analyze "
                                "all files in the src/auth directory for dependencies. Find circular "
                                "dependencies and output a Mermaid diagram. "
                                "This task is complex and requires delegation."
                            ),
                            intent="analysis")
        assert _parent_called_task(log_dir), "LLM should delegate a complex analysis task"

    def test_negation_respects_no_delegation(self, tmp_path):
        runtime, store, log_dir = _make_real_runtime(tmp_path)
        root = runtime.create_root_session(agent_name="build", repo_path=str(tmp_path), title="negation")
        runtime.run_session(root.id, agent_name="build",
                            task_description=(
                                "Do NOT use the task tool. Do NOT delegate. "
                                "Tell me directly: Python list vs tuple difference."
                            ),
                            intent="analysis")
        assert not _parent_called_task(log_dir), "LLM should respect explicit no-delegation"
