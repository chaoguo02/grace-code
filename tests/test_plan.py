"""
tests/test_plan.py

测试 Plan-and-Execute 模式：
- Plan.from_json 解析（正常 / markdown 包裹 / 异常）
- PlanExecuteAgent 主流程（MockBackend）
- 降级 fallback
- Event log 事件完整性
"""

from __future__ import annotations

import pytest

from agent.plan import (
    Plan, PlanExecuteConfig, PlanGenerationError, SubTask,
)
from agent.task import (
    Action, ActionType, EventType, RunStatus, Task, ToolCall,
)
from agent.core import AgentConfig, PlanExecuteAgent
from agent.event_log import EventLog
from llm.base import MockBackend
from tools.base import NoopTool, ToolRegistry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def task(tmp_path) -> Task:
    return Task(
        task_id="plan001",
        description="Fix the parser bug in src/parser.py and add a unit test for it.",
        repo_path=str(tmp_path),
        max_steps=10,
    )


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry().register(NoopTool("shell"))


# ===========================================================================
# Plan.from_json
# ===========================================================================

VALID_PLAN_JSON = """\
{
  "reasoning": "First fix the code, then verify with tests",
  "plan": [
    {
      "id": "1",
      "description": "Read src/parser.py to understand the bug",
      "expected_outcome": "Identified the root cause"
    },
    {
      "id": "2",
      "description": "Edit src/parser.py to fix the bug",
      "expected_outcome": "Parser no longer crashes on empty input"
    },
    {
      "id": "3",
      "description": "Run pytest to verify the fix",
      "expected_outcome": "All tests pass"
    }
  ]
}
"""


class TestPlanFromJson:
    def test_valid_json(self):
        plan = Plan.from_json(VALID_PLAN_JSON, "Fix parser bug")
        assert len(plan.subtasks) == 3
        assert plan.reasoning == "First fix the code, then verify with tests"
        assert plan.original_task == "Fix parser bug"
        assert plan.subtasks[0].id == "1"
        assert plan.subtasks[0].description == "Read src/parser.py to understand the bug"
        assert plan.subtasks[0].expected_outcome == "Identified the root cause"

    def test_markdown_fence(self):
        text = "```json\n" + VALID_PLAN_JSON + "\n```"
        plan = Plan.from_json(text, "task")
        assert len(plan.subtasks) == 3

    def test_markdown_fence_no_lang(self):
        text = "```\n" + VALID_PLAN_JSON + "\n```"
        plan = Plan.from_json(text, "task")
        assert len(plan.subtasks) == 3

    def test_extra_text_around_json(self):
        text = "Here is my plan:\n" + VALID_PLAN_JSON + "\nI hope this works."
        plan = Plan.from_json(text, "task")
        assert len(plan.subtasks) == 3

    def test_invalid_json_raises(self):
        # "this is not json at all" — 没有花括号，触发 "No JSON object" 错误
        with pytest.raises(PlanGenerationError, match="No JSON object"):
            Plan.from_json("this is not json at all", "task")

    def test_missing_plan_key_raises(self):
        with pytest.raises(PlanGenerationError, match="missing 'plan'"):
            Plan.from_json('{"reasoning": "no plan here"}', "task")

    def test_empty_plan_raises(self):
        with pytest.raises(PlanGenerationError, match="at least one"):
            Plan.from_json('{"plan": []}', "task")

    def test_subtask_missing_id_raises(self):
        with pytest.raises(PlanGenerationError, match="missing 'id'"):
            Plan.from_json(
                '{"plan": [{"description": "do something"}]}',
                "task",
            )

    def test_subtask_missing_description_raises(self):
        with pytest.raises(PlanGenerationError, match="missing 'id'"):
            Plan.from_json('{"plan": [{"id": "1"}]}', "task")

    def test_single_subtask_ok(self):
        plan = Plan.from_json(
            '{"plan": [{"id": "1", "description": "simple fix"}]}',
            "task",
        )
        assert len(plan.subtasks) == 1

    def test_no_reasoning_is_ok(self):
        plan = Plan.from_json(
            '{"plan": [{"id": "1", "description": "fix it"}]}',
            "task",
        )
        assert plan.reasoning == ""

    def test_to_dict(self):
        plan = Plan.from_json(VALID_PLAN_JSON, "Fix parser bug")
        d = plan.to_dict()
        assert d["original_task"] == "Fix parser bug"
        assert len(d["subtasks"]) == 3


# ===========================================================================
# SubTask.to_dict
# ===========================================================================

class TestSubTask:
    def test_to_dict_contains_all_fields(self):
        st = SubTask(id="5", description="Run tests", expected_outcome="All pass")
        d = st.to_dict()
        assert d == {"id": "5", "description": "Run tests", "expected_outcome": "All pass"}

    def test_repr(self):
        st = SubTask(id="1", description="Fix the parser bug in src/parser.py")
        assert "Fix the parser" in repr(st)


# ===========================================================================
# PlanExecuteAgent
# ===========================================================================

class TestPlanExecuteAgent:

    def _make_plan_json(self, subtask_count: int = 2) -> str:
        """生成指定数量的 subtask JSON。"""
        import json
        plan_data = {
            "reasoning": "Step by step",
            "plan": [
                {"id": str(i), "description": f"Step {i}", "expected_outcome": f"Done {i}"}
                for i in range(1, subtask_count + 1)
            ],
        }
        return json.dumps(plan_data)

    def _make_finish_for_plan_json(self, plan_json: str) -> Action:
        """构造一个 FINISH action，其 message 包含 plan JSON。
        这是对 planning LLM 调用的模拟——planning 调用 tools=[]，
        LLM 以 FINISH 返回 JSON 文本。
        """
        return Action(
            action_type=ActionType.FINISH,
            thought="",
            message=plan_json,
        )

    def _make_plan_execute_agent(
        self, script: list[Action], registry=None, agent_config=None
    ) -> PlanExecuteAgent:
        """创建 PlanExecuteAgent，planning 调用用 mock 脚本。"""
        backend = MockBackend(script)
        return PlanExecuteAgent(
            backend, registry or ToolRegistry().register(NoopTool("shell")),
            agent_config=agent_config,
        )

    def test_complete_run_succeeds(self, tmp_path, task):
        """完整 plan+execute 流程：生成 plan → 依次执行 → 成功。"""
        registry = ToolRegistry().register(NoopTool("shell"))
        plan_json = self._make_plan_json(2)

        # Script:
        #   1st call: planning → FINISH with JSON plan
        #   2nd call: subtask 1 → TOOL_CALL then FINISH
        #   3rd call: subtask 2 → TOOL_CALL then FINISH
        script = [
            self._make_finish_for_plan_json(plan_json),     # planning
            Action(ActionType.TOOL_CALL, "step1", ToolCall("shell", {"cmd": "ls"})),  # sub1
            Action(ActionType.FINISH, "done1", message="ok1"),
            Action(ActionType.TOOL_CALL, "step2", ToolCall("shell", {"cmd": "pwd"})),  # sub2
            Action(ActionType.FINISH, "done2", message="ok2"),
        ]
        backend = MockBackend(script)
        agent = PlanExecuteAgent(backend, registry, AgentConfig(max_steps=5))

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        assert result.status == RunStatus.SUCCESS
        # 2 subtasks, each: 1 TOOL_CALL + 1 FINISH = 2 steps → 4 total
        assert result.steps_taken == 4
        assert "2/2" in result.summary

    def test_plan_generated_event_logged(self, tmp_path, task, registry):
        """父级 EventLog 应包含 PLAN_GENERATED 事件。"""
        plan_json = self._make_plan_json(1)

        script = [
            self._make_finish_for_plan_json(plan_json),
            Action(ActionType.FINISH, "done", message="ok"),
        ]
        backend = MockBackend(script)
        agent = PlanExecuteAgent(backend, registry, AgentConfig(max_steps=5))

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            agent.run(task, log)
            events = log.replay()

        plan_events = [e for e in events if e.event_type == EventType.PLAN_GENERATED]
        assert len(plan_events) == 1
        plan_payload = plan_events[0].payload["plan"]
        assert len(plan_payload["subtasks"]) == 1

    def test_subtask_events_logged(self, tmp_path, task, registry):
        """父级 EventLog 应包含 SUBTASK_START / SUBTASK_COMPLETE 事件。"""
        plan_json = self._make_plan_json(2)

        script = [
            self._make_finish_for_plan_json(plan_json),
            Action(ActionType.FINISH, "done1", message="ok1"),
            Action(ActionType.FINISH, "done2", message="ok2"),
        ]
        backend = MockBackend(script)
        agent = PlanExecuteAgent(backend, registry, AgentConfig(max_steps=5))

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            agent.run(task, log)
            events = log.replay()

        start_events = [e for e in events if e.event_type == EventType.SUBTASK_START]
        complete_events = [e for e in events if e.event_type == EventType.SUBTASK_COMPLETE]
        assert len(start_events) == 2
        assert len(complete_events) == 2

    def test_plan_failed_fallback_to_react(self, tmp_path, task, registry):
        """plan 生成失败时降级为纯 ReAct。"""
        # planning 调用返回非 JSON 的 GIVE_UP → 解析失败 → fallback
        script = [
            Action(ActionType.GIVE_UP, "cannot plan", message="I can't plan"),
            Action(ActionType.TOOL_CALL, "react_step", ToolCall("shell", {"cmd": "ls"})),
            Action(ActionType.FINISH, "react_done", message="Fixed via ReAct"),
        ]
        backend = MockBackend(script)
        agent = PlanExecuteAgent(backend, registry, AgentConfig(max_steps=5))

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        # fallback to ReActAgent — should succeed
        assert result.status == RunStatus.SUCCESS
        assert "Fixed via ReAct" in result.summary

    def test_subtask_give_up_stops(self, tmp_path, task, registry):
        """第一个 subtask GAVE_UP 时提前终止，不执行后续 subtask。"""
        plan_json = self._make_plan_json(3)

        script = [
            self._make_finish_for_plan_json(plan_json),
            Action(ActionType.GIVE_UP, "stuck", message="Cannot fix this part"),
            # subtask 2, 3 不应该被执行
        ]
        backend = MockBackend(script)
        agent = PlanExecuteAgent(backend, registry, AgentConfig(max_steps=5))

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)
            events = log.replay()

        assert result.status == RunStatus.GAVE_UP
        assert "aborted" in result.summary.lower()

        # 应该只有第一个 subtask 的 START 事件
        start_events = [e for e in events if e.event_type == EventType.SUBTASK_START]
        assert len(start_events) == 1

        failed_events = [e for e in events if e.event_type == EventType.SUBTASK_FAILED]
        assert len(failed_events) == 1

    def test_subtask_max_steps_stops(self, tmp_path, registry):
        """subtask 达到 MAX_STEPS 时提前终止。"""
        plan_json = self._make_plan_json(2)

        # subtask 需要 max_steps=3，配 5 个 TOOL_CALL
        task = Task(
            task_id="maxsteps",
            description="Fix",
            repo_path=str(tmp_path),
            max_steps=3,  # subtask 继承此值
        )

        many_steps = [
            Action(ActionType.TOOL_CALL, f"s{i}", ToolCall("shell", {"cmd": f"echo {i}"}))
            for i in range(5)
        ]
        script = [
            self._make_finish_for_plan_json(plan_json),
            *many_steps,   # 5 步 TOOL_CALL，subtask max_steps=3 → MAX_STEPS
        ]
        backend = MockBackend(script)
        agent = PlanExecuteAgent(
            backend, registry,
            AgentConfig(max_steps=3, loop_detection_window=10),
        )

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        assert result.status == RunStatus.MAX_STEPS

    def test_subtask_log_created(self, tmp_path, task, registry):
        """每个 subtask 应生成独立的 EventLog 文件。"""
        plan_json = self._make_plan_json(2)

        script = [
            self._make_finish_for_plan_json(plan_json),
            Action(ActionType.FINISH, "done1", message="ok1"),
            Action(ActionType.FINISH, "done2", message="ok2"),
        ]
        backend = MockBackend(script)
        plan_cfg = PlanExecuteConfig(
            plan_subtask_log_dir=str(tmp_path / "subtask_logs"),
        )
        agent = PlanExecuteAgent(backend, registry, AgentConfig(max_steps=5), plan_cfg)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            agent.run(task, log)

        import os
        subtask_log_dir = tmp_path / "subtask_logs"
        log_files = list(subtask_log_dir.glob("*.jsonl"))
        assert len(log_files) >= 2  # 每个 subtask 一个文件

    def test_plan_config_defaults(self):
        """PlanExecuteConfig 默认值。"""
        cfg = PlanExecuteConfig()
        assert cfg.plan_max_subtasks == 10
        assert cfg.plan_subtask_log_dir == "./logs/subtasks"

    def test_agent_config_not_modified(self, tmp_path, task, registry):
        """PlanExecuteAgent 不应修改传入的 AgentConfig。"""
        plan_json = self._make_plan_json(1)
        script = [
            self._make_finish_for_plan_json(plan_json),
            Action(ActionType.FINISH, "done", message="ok"),
        ]
        backend = MockBackend(script)
        cfg = AgentConfig(max_steps=42, budget_tokens=99999)
        agent = PlanExecuteAgent(backend, registry, cfg)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            agent.run(task, log)

        # 原始 config 不应被修改
        assert cfg.max_steps == 42
        assert cfg.budget_tokens == 99999

    def test_patch_included_on_success(self, tmp_path):
        """PlanExecuteAgent 成功时应包含 git diff patch。"""
        import subprocess

        # 初始化 git repo
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "t@t.com"],
            cwd=tmp_path, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"],
            cwd=tmp_path, capture_output=True,
        )
        f = tmp_path / "main.py"
        f.write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=tmp_path, capture_output=True,
        )

        # 修改文件（模拟 agent 编辑）
        f.write_text("x = 99\n")

        task = Task(
            task_id="patch_plan",
            description="Fix",
            repo_path=str(tmp_path),
            max_steps=5,
        )
        plan_json = self._make_plan_json(1)
        script = [
            self._make_finish_for_plan_json(plan_json),
            Action(ActionType.FINISH, "done", message="Fixed"),
        ]
        backend = MockBackend(script)
        registry = ToolRegistry().register(NoopTool("shell"))
        agent = PlanExecuteAgent(backend, registry, AgentConfig(max_steps=5))

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        assert result.is_success()
        assert result.patch is not None
        assert "main.py" in result.patch


# ===========================================================================
# PlanExecuteAgent — planning LLM 异常
# ===========================================================================

class TestPlanExecuteAgentLLMError:
    def test_backend_exception_on_planning(self, tmp_path, task, registry):
        """planning LLM 调用抛异常时，应日志失败并返回 FAILED。"""
        class FailingBackend(MockBackend):
            def complete(self, messages, tools):
                raise ConnectionError("API unreachable")

        backend = FailingBackend([])
        agent = PlanExecuteAgent(backend, registry, AgentConfig(max_steps=5))

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        # LLM 异常 → plan 生成失败 → 降级 → 但降级的 ReActAgent 也用同一个 backend
        # 所以也会抛异常，整个 run 返回 FAILED
        assert result.status == RunStatus.FAILED
