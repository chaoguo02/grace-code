"""
tests/test_plan.py

测试 Plan-and-Execute 模式（Claude Code 风格）：
- Plan 数据结构（JSON 兼容 + markdown 新格式）
- PlanExecuteAgent 两阶段流程
- 用户审批流程
- 只读工具限制
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
        max_steps=15,
    )


@pytest.fixture
def registry() -> ToolRegistry:
    return (
        ToolRegistry()
        .register(NoopTool("shell"))
        .register(NoopTool("file_read"))
        .register(NoopTool("file_view"))
        .register(NoopTool("file_write"))
        .register(NoopTool("find_files"))
        .register(NoopTool("search_text"))
    )


# ===========================================================================
# Plan.from_json — 旧格式兼容
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
# Plan.from_markdown — 新格式
# ===========================================================================

class TestPlanFromMarkdown:
    def test_basic_markdown(self):
        md = "### Analysis\nFound bug in parser.py\n### Changes\nFix line 42"
        plan = Plan.from_markdown(md, "Fix parser")
        assert plan.is_markdown_plan
        assert plan.original_task == "Fix parser"
        assert "Found bug" in plan.plan_text
        assert len(plan.subtasks) == 0

    def test_plan_text_property(self):
        md = "Step 1: Read files\nStep 2: Edit code"
        plan = Plan.from_markdown(md, "task")
        assert plan.plan_text == md

    def test_repr_markdown(self):
        plan = Plan.from_markdown("short plan", "task")
        assert "markdown" in repr(plan)

    def test_is_markdown_false_for_json(self):
        plan = Plan.from_json(
            '{"plan": [{"id": "1", "description": "x"}]}', "task"
        )
        assert not plan.is_markdown_plan


# ===========================================================================
# SubTask.to_dict
# ===========================================================================

class TestSubTask:
    def test_to_dict_contains_all_fields(self):
        st = SubTask(id="5", description="Run tests", expected_outcome="All pass")
        d = st.to_dict()
        assert d["id"] == "5"
        assert d["description"] == "Run tests"
        assert d["expected_outcome"] == "All pass"
        assert d["result_summary"] == ""

    def test_repr(self):
        st = SubTask(id="1", description="Fix the parser bug in src/parser.py")
        assert "Fix the parser" in repr(st)


# ===========================================================================
# PlanExecuteAgent — 新版两阶段
# ===========================================================================

class TestPlanExecuteAgent:

    def test_plan_and_execute_with_approval(self, tmp_path, task, registry):
        """完整两阶段流程：规划 → 审批 → 执行 → 成功。"""
        # Phase 1: plan agent 探索后 FINISH with plan text
        # Phase 2: exec agent 执行后 FINISH
        script = [
            # Phase 1 (plan): read a file, then finish with plan
            Action(ActionType.TOOL_CALL, "exploring", ToolCall("file_read", {"path": "x.py"})),
            Action(ActionType.FINISH, "plan done", message="### Changes\nFix line 10 in x.py"),
            # Phase 2 (exec): make edit, then finish
            Action(ActionType.TOOL_CALL, "fixing", ToolCall("file_write", {"path": "x.py", "content": "fixed"})),
            Action(ActionType.FINISH, "executed", message="Fixed the bug"),
        ]
        backend = MockBackend(script)

        plan_cfg = PlanExecuteConfig(
            plan_subtask_log_dir=str(tmp_path / "plan_logs"),
            plan_approval_callback=lambda text: True,  # auto-approve
        )
        agent = PlanExecuteAgent(backend, registry, AgentConfig(max_steps=15), plan_cfg)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        assert result.is_success()
        assert "Fixed the bug" in result.summary

    def test_plan_rejected_returns_gave_up(self, tmp_path, task, registry):
        """用户拒绝 plan 时返回 GAVE_UP。"""
        script = [
            Action(ActionType.FINISH, "plan", message="### Plan\nDo stuff"),
        ]
        backend = MockBackend(script)

        plan_cfg = PlanExecuteConfig(
            plan_subtask_log_dir=str(tmp_path / "plan_logs"),
            plan_approval_callback=lambda text: False,  # reject
        )
        agent = PlanExecuteAgent(backend, registry, AgentConfig(max_steps=15), plan_cfg)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        assert result.status == RunStatus.GAVE_UP
        assert "rejected" in result.summary.lower()

    def test_no_approval_callback_auto_executes(self, tmp_path, task, registry):
        """没有审批回调时自动执行（向后兼容）。"""
        script = [
            Action(ActionType.FINISH, "plan", message="### Plan\nFix it"),
            Action(ActionType.FINISH, "done", message="Fixed"),
        ]
        backend = MockBackend(script)

        plan_cfg = PlanExecuteConfig(
            plan_subtask_log_dir=str(tmp_path / "plan_logs"),
        )
        agent = PlanExecuteAgent(backend, registry, AgentConfig(max_steps=15), plan_cfg)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        assert result.is_success()

    def test_plan_generated_event_logged(self, tmp_path, task, registry):
        """父级 EventLog 应包含 PLAN_GENERATED 事件。"""
        script = [
            Action(ActionType.FINISH, "plan", message="My plan here"),
            Action(ActionType.FINISH, "done", message="ok"),
        ]
        backend = MockBackend(script)

        plan_cfg = PlanExecuteConfig(
            plan_subtask_log_dir=str(tmp_path / "plan_logs"),
            plan_approval_callback=lambda t: True,
        )
        agent = PlanExecuteAgent(backend, registry, AgentConfig(max_steps=15), plan_cfg)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            agent.run(task, log)
            events = log.replay()

        plan_events = [e for e in events if e.event_type == EventType.PLAN_GENERATED]
        assert len(plan_events) == 1

    def test_readonly_registry_in_phase1(self, tmp_path, task):
        """Phase 1 应该只有只读工具可用。"""
        registry = (
            ToolRegistry()
            .register(NoopTool("shell"))
            .register(NoopTool("file_read"))
            .register(NoopTool("file_write"))
            .register(NoopTool("find_files"))
            .register(NoopTool("search_text"))
        )

        backend = MockBackend([
            Action(ActionType.FINISH, "plan", message="plan text"),
            Action(ActionType.FINISH, "done", message="ok"),
        ])

        plan_cfg = PlanExecuteConfig(
            plan_subtask_log_dir=str(tmp_path / "plan_logs"),
            plan_approval_callback=lambda t: True,
        )
        agent = PlanExecuteAgent(backend, registry, AgentConfig(max_steps=15), plan_cfg)

        readonly = agent._make_readonly_registry()
        tool_names = set(readonly._tools.keys())

        # 只读工具应该保留
        assert "file_read" in tool_names
        assert "find_files" in tool_names
        assert "search_text" in tool_names
        # 写入工具应该被过滤
        assert "shell" not in tool_names
        assert "file_write" not in tool_names

    def test_empty_plan_falls_back(self, tmp_path, task, registry):
        """Plan 为空时降级到 ReActAgent。"""
        script = [
            # Phase 1 plan agent gives up (empty summary)
            Action(ActionType.GIVE_UP, "stuck", message=""),
            # Fallback ReActAgent runs
            Action(ActionType.FINISH, "direct", message="Done directly"),
        ]
        backend = MockBackend(script)

        plan_cfg = PlanExecuteConfig(
            plan_subtask_log_dir=str(tmp_path / "plan_logs"),
        )
        agent = PlanExecuteAgent(backend, registry, AgentConfig(max_steps=15), plan_cfg)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        assert result.is_success()
        assert "Done directly" in result.summary

    def test_plan_config_defaults(self):
        """PlanExecuteConfig 默认值。"""
        cfg = PlanExecuteConfig()
        assert cfg.plan_max_subtasks == 10
        assert cfg.plan_subtask_log_dir == "./logs/subtasks"
        assert cfg.plan_approval_callback is None

    def test_execution_phase_failure(self, tmp_path, task, registry):
        """执行阶段失败时正确返回错误状态。"""
        script = [
            Action(ActionType.FINISH, "plan", message="### Plan\nFix it"),
            Action(ActionType.GIVE_UP, "stuck", message="Cannot fix"),
        ]
        backend = MockBackend(script)

        plan_cfg = PlanExecuteConfig(
            plan_subtask_log_dir=str(tmp_path / "plan_logs"),
            plan_approval_callback=lambda t: True,
        )
        agent = PlanExecuteAgent(backend, registry, AgentConfig(max_steps=15), plan_cfg)

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        assert result.status == RunStatus.GAVE_UP
        assert "Cannot fix" in result.summary


# ===========================================================================
# PlanExecuteAgent — LLM 异常
# ===========================================================================

class TestPlanExecuteAgentLLMError:
    def test_backend_exception_on_planning(self, tmp_path, task, registry):
        """planning LLM 调用抛异常时，应降级到 ReAct。"""
        class FailingBackend(MockBackend):
            def complete(self, messages, tools):
                raise ConnectionError("API unreachable")

        backend = FailingBackend([])
        agent = PlanExecuteAgent(backend, registry, AgentConfig(max_steps=5))

        with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
            result = agent.run(task, log)

        # Both plan agent and fallback use same failing backend
        assert result.status == RunStatus.FAILED


# ===========================================================================
# Factory integration
# ===========================================================================

class TestFactoryPlanMode:
    def test_factory_creates_plan_agent(self):
        from agent.factory import create_agent
        backend = MockBackend([])
        registry = ToolRegistry().register(NoopTool("shell"))
        agent = create_agent("plan", backend, registry)
        assert isinstance(agent, PlanExecuteAgent)

    def test_factory_passes_approval_callback(self):
        from agent.factory import create_agent
        backend = MockBackend([])
        registry = ToolRegistry().register(NoopTool("shell"))
        cb = lambda text: True
        agent = create_agent("plan", backend, registry, plan_approval_callback=cb)
        assert agent._plan_cfg.plan_approval_callback is cb

    def test_factory_auto_mode_simple_task(self):
        from agent.factory import create_agent
        from agent.core import ReActAgent
        backend = MockBackend([])
        registry = ToolRegistry().register(NoopTool("shell"))
        agent = create_agent("auto", backend, registry, task_description="fix typo")
        assert isinstance(agent, ReActAgent)

    def test_factory_auto_mode_complex_task(self):
        from agent.factory import create_agent
        backend = MockBackend([])
        registry = ToolRegistry().register(NoopTool("shell"))
        agent = create_agent(
            "auto", backend, registry,
            task_description="1. Refactor the authentication module\n2. Add tests\n3. Update docs\n4. Deploy",
        )
        assert isinstance(agent, PlanExecuteAgent)
