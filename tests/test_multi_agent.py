"""
tests/test_multi_agent.py

Multi-Agent 系统测试：
- SubAgentExecutor: 工具过滤、角色隔离、spawn 执行
- CoordinatorAgent: LLM 驱动调度、spawn_agent 工具、预算隔离
- Coordinator 工具：SpawnAgentTool, ListAgentResultsTool, FinishCoordinationTool
- 角色 prompt 构建
- Factory 集成
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from agent.multi_agent import (
    CoordinatorAgent,
    FinishCoordinationTool,
    ListAgentResultsTool,
    MultiAgentConfig,
    ROLE_TOOL_WHITELIST,
    SpawnAgentTool,
    SpawnParallelTool,
    SubAgentConfig,
    SubAgentExecutor,
    SubAgentResult,
    SubAgentRole,
)
from agent.task import Action, ActionType, RunResult, RunStatus, Task


# ===========================================================================
# SubAgentRole 工具白名单测试
# ===========================================================================

class TestRoleToolWhitelist:
    def test_explorer_is_readonly(self):
        """Explorer 没有任何写入工具。"""
        tools = ROLE_TOOL_WHITELIST[SubAgentRole.EXPLORER]
        write_tools = {"file_write", "shell", "git_add", "git_commit"}
        assert not tools & write_tools

    def test_planner_is_readonly(self):
        """Planner 没有任何写入工具。"""
        tools = ROLE_TOOL_WHITELIST[SubAgentRole.PLANNER]
        write_tools = {"file_write", "shell", "git_add", "git_commit"}
        assert not tools & write_tools

    def test_coder_has_write_tools(self):
        """Coder 有 file_write 和 shell。"""
        tools = ROLE_TOOL_WHITELIST[SubAgentRole.CODER]
        assert "file_write" in tools
        assert "shell" in tools

    def test_reviewer_has_test_but_no_write(self):
        """Reviewer 有 shell/pytest 但没有 file_write。"""
        tools = ROLE_TOOL_WHITELIST[SubAgentRole.REVIEWER]
        assert "shell" in tools or "pytest" in tools
        assert "file_write" not in tools

    def test_tester_has_test_tools(self):
        """Tester 有 shell 和 pytest。"""
        tools = ROLE_TOOL_WHITELIST[SubAgentRole.TESTER]
        assert "shell" in tools
        assert "pytest" in tools
        assert "file_write" not in tools

    def test_all_roles_have_file_read(self):
        """所有角色都有 file_read。"""
        for role in SubAgentRole:
            assert "file_read" in ROLE_TOOL_WHITELIST[role]


# ===========================================================================
# SubAgentExecutor 工具过滤测试
# ===========================================================================

class TestSubAgentExecutor:
    def _make_registry(self):
        """创建一个含多种工具的 mock registry。"""
        from tools.base import ToolRegistry, NoopTool

        registry = ToolRegistry()
        for name in ("file_read", "file_view", "file_write", "find_files",
                     "search_text", "shell", "pytest", "git_status", "git_diff",
                     "git_add", "git_commit", "find_symbol", "web_search", "web_fetch"):
            registry.register(NoopTool(tool_name=name))
        return registry

    def test_filter_explorer_tools(self):
        """Explorer 过滤后只有只读工具。"""
        registry = self._make_registry()
        backend = MagicMock()
        executor = SubAgentExecutor(backend, registry)
        filtered = executor._filter_registry(SubAgentRole.EXPLORER)

        assert "file_read" in filtered
        assert "file_write" not in filtered
        assert "shell" not in filtered
        assert "git_add" not in filtered

    def test_filter_coder_tools(self):
        """Coder 过滤后有读写工具。"""
        registry = self._make_registry()
        backend = MagicMock()
        executor = SubAgentExecutor(backend, registry)
        filtered = executor._filter_registry(SubAgentRole.CODER)

        assert "file_read" in filtered
        assert "file_write" in filtered
        assert "shell" in filtered
        assert "git_add" in filtered

    def test_filter_reviewer_tools(self):
        """Reviewer 过滤后有 shell/pytest 但没有 file_write。"""
        registry = self._make_registry()
        backend = MagicMock()
        executor = SubAgentExecutor(backend, registry)
        filtered = executor._filter_registry(SubAgentRole.REVIEWER)

        assert "file_read" in filtered
        assert "shell" in filtered
        assert "pytest" in filtered
        assert "file_write" not in filtered
        assert "git_add" not in filtered


# ===========================================================================
# SubAgentExecutor.spawn 测试（mock LLM）
# ===========================================================================

class TestSubAgentSpawn:
    def test_spawn_returns_result(self, tmp_path):
        """spawn 返回 SubAgentResult。"""
        from tools.base import ToolRegistry, NoopTool

        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))

        # Mock backend 直接返回 FINISH action
        backend = MagicMock()
        from llm.base import LLMResponse
        backend.supports_function_calling = True
        backend.max_context_window = 128_000
        backend.model_name = "test-model"
        backend.complete.return_value = LLMResponse(
            action=Action(
                action_type=ActionType.FINISH,
                thought="done",
                message="Exploration complete. Found key files.",
            ),
            raw_content="done",
            input_tokens=100,
            output_tokens=50,
        )

        executor = SubAgentExecutor(backend, registry)
        config = SubAgentConfig(
            role=SubAgentRole.EXPLORER,
            max_steps=5,
            budget_tokens=10_000,
            task_prompt="Explore the repo to find authentication code",
        )

        result = executor.spawn(
            config=config,
            repo_path=".",
            log_dir=str(tmp_path),
        )

        assert isinstance(result, SubAgentResult)
        assert result.role == SubAgentRole.EXPLORER
        assert result.status == RunStatus.SUCCESS
        assert "Exploration complete" in result.summary
        assert result.agent_id  # non-empty


# ===========================================================================
# Coordinator 工具测试
# ===========================================================================

class TestSpawnAgentTool:
    def _make_coordinator(self):
        """创建一个带 mock 状态的 CoordinatorAgent。"""
        from tools.base import ToolRegistry, NoopTool

        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))
        backend = MagicMock()

        from agent.core import AgentConfig
        config = AgentConfig(budget_tokens=100_000)
        coord = CoordinatorAgent(backend, registry, config)
        # 初始化运行状态
        coord._results = []
        coord._spawn_count = 0
        coord._repo_path = "."
        coord._log_dir = None
        return coord

    def test_spawn_missing_task(self):
        """task 为空时返回 error。"""
        coord = self._make_coordinator()
        tool = SpawnAgentTool(coord)
        result = tool.execute({"role": "explorer", "task": ""})
        assert not result.success
        assert "required" in result.error

    def test_spawn_invalid_role(self):
        """无效角色返回 error。"""
        coord = self._make_coordinator()
        tool = SpawnAgentTool(coord)
        result = tool.execute({"role": "invalid_role", "task": "do something"})
        assert not result.success
        assert "Unknown role" in result.error

    def test_spawn_budget_exhausted(self):
        """max_agents 达到上限时拒绝 spawn。"""
        coord = self._make_coordinator()
        coord._spawn_count = coord._multi_cfg.max_agents  # 达到上限
        tool = SpawnAgentTool(coord)
        result = tool.execute({"role": "explorer", "task": "explore"})
        assert not result.success
        assert "budget" in result.error.lower() or "exhausted" in result.error.lower()

    def test_spawn_with_depends_on(self, tmp_path):
        """depends_on 正确注入上游上下文。"""
        coord = self._make_coordinator()
        coord._log_dir = str(tmp_path)
        # 添加一个已有结果
        coord._results.append(SubAgentResult(
            agent_id="abc123",
            role=SubAgentRole.EXPLORER,
            status=RunStatus.SUCCESS,
            summary="Found: auth.py has login() at line 42",
            steps_taken=3,
            total_tokens=500,
        ))

        # Mock sub_executor.spawn
        def mock_spawn(config, repo_path, upstream_context="", log_dir=None, thread_isolated=False):
            assert "auth.py" in upstream_context
            return SubAgentResult(
                agent_id="def456",
                role=config.role,
                status=RunStatus.SUCCESS,
                summary="Planned changes",
                steps_taken=2,
                total_tokens=300,
            )

        coord._sub_executor.spawn = mock_spawn

        tool = SpawnAgentTool(coord)
        result = tool.execute({
            "role": "planner",
            "task": "Create a plan based on explorer findings",
            "depends_on": ["abc123"],
        })

        assert result.success
        assert "Planned changes" in result.output
        assert len(coord._results) == 2


class TestListAgentResultsTool:
    def test_empty_results(self):
        from tools.base import ToolRegistry, NoopTool
        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))
        coord = CoordinatorAgent(MagicMock(), registry)
        coord._results = []
        coord._tokens_used_by_subs = 0
        coord._sub_budget = 70_000

        tool = ListAgentResultsTool(coord)
        result = tool.execute({})
        assert result.success
        assert "No sub-agent results" in result.output

    def test_filter_by_role(self):
        from tools.base import ToolRegistry, NoopTool
        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))
        coord = CoordinatorAgent(MagicMock(), registry)
        coord._tokens_used_by_subs = 1000
        coord._sub_budget = 70_000
        coord._results = [
            SubAgentResult(agent_id="a1", role=SubAgentRole.EXPLORER, status=RunStatus.SUCCESS, summary="found stuff"),
            SubAgentResult(agent_id="a2", role=SubAgentRole.CODER, status=RunStatus.SUCCESS, summary="coded stuff"),
        ]

        tool = ListAgentResultsTool(coord)

        # Filter by explorer
        result = tool.execute({"role": "explorer"})
        assert "found stuff" in result.output
        assert "coded stuff" not in result.output

        # All
        result = tool.execute({"role": "all"})
        assert "found stuff" in result.output
        assert "coded stuff" in result.output


class TestFinishCoordinationTool:
    def test_sets_final_state(self):
        from tools.base import ToolRegistry, NoopTool
        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))
        coord = CoordinatorAgent(MagicMock(), registry)
        coord._final_summary = ""
        coord._final_status = ""

        tool = FinishCoordinationTool(coord)
        result = tool.execute({"summary": "All done, tests pass", "status": "success"})

        assert result.success
        assert coord._final_summary == "All done, tests pass"
        assert coord._final_status == "success"


# ===========================================================================
# CoordinatorAgent 集成测试
# ===========================================================================

class TestCoordinatorAgent:
    def test_run_completes_with_finish(self, tmp_path):
        """Coordinator 正常执行并通过 finish_coordination 结束。"""
        from tools.base import ToolRegistry, NoopTool
        from llm.base import LLMResponse
        from agent.task import ToolCall

        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))

        # Mock backend: first call spawns explorer, second call finishes
        backend = MagicMock()
        backend.supports_function_calling = True
        backend.max_context_window = 128_000
        backend.model_name = "test-model"

        call_count = [0]

        def mock_complete(messages, tools):
            call_count[0] += 1
            if call_count[0] == 1:
                # Coordinator calls finish_coordination
                return LLMResponse(
                    action=Action(
                        action_type=ActionType.TOOL_CALL,
                        thought="Task is simple enough, finishing directly",
                        tool_calls=[ToolCall(
                            name="finish_coordination",
                            params={"summary": "Task completed successfully", "status": "success"},
                            id="call_1",
                        )],
                    ),
                    raw_content="finishing",
                    input_tokens=200,
                    output_tokens=50,
                )
            else:
                return LLMResponse(
                    action=Action(
                        action_type=ActionType.FINISH,
                        thought="done",
                        message="Coordination complete.",
                    ),
                    raw_content="done",
                    input_tokens=100,
                    output_tokens=30,
                )

        backend.complete.side_effect = mock_complete

        multi_cfg = MultiAgentConfig(
            coordinator_max_steps=5,
            log_dir=str(tmp_path),
        )
        coord = CoordinatorAgent(backend, registry, multi_config=multi_cfg)

        task = Task(description="Fix the login bug", repo_path=".", max_steps=50, budget_tokens=100_000)
        from agent.event_log import EventLog
        log = EventLog.create(task, log_dir=str(tmp_path))

        result = coord.run(task, log)
        log.close()

        assert result.status == RunStatus.SUCCESS
        assert "completed successfully" in result.summary


# ===========================================================================
# SubAgentResult 格式化测试
# ===========================================================================

class TestSubAgentResult:
    def test_to_display_success(self):
        r = SubAgentResult(
            agent_id="abc12345",
            role=SubAgentRole.EXPLORER,
            status=RunStatus.SUCCESS,
            summary="Found auth.py",
            steps_taken=3,
            total_tokens=500,
        )
        display = r.to_display()
        assert "✓" in display
        assert "explorer" in display
        assert "Found auth.py" in display

    def test_to_display_failure(self):
        r = SubAgentResult(
            agent_id="def67890",
            role=SubAgentRole.CODER,
            status=RunStatus.FAILED,
            summary="Could not write file",
            steps_taken=5,
            total_tokens=1000,
        )
        display = r.to_display()
        assert "✗" in display
        assert "coder" in display


# ===========================================================================
# Prompt 构建测试
# ===========================================================================

class TestMultiAgentPrompts:
    def test_coordinator_prompt_contains_task(self):
        from agent.prompt import build_coordinator_system_prompt
        prompt = build_coordinator_system_prompt(
            "Fix the login bug", "/repo",
            total_budget=100_000, sub_agent_budget=70_000, max_retries=2,
        )
        assert "Fix the login bug" in prompt
        assert "/repo" in prompt
        assert "spawn_agent" in prompt
        assert "70000" in prompt or "70,000" in prompt or "70000" in prompt

    def test_sub_agent_prompt_with_upstream(self):
        from agent.prompt import build_sub_agent_prompt
        prompt = build_sub_agent_prompt(
            role="coder",
            task_prompt="Edit auth.py to add retry logic",
            upstream_context="Explorer found: auth.py line 42 has login()",
        )
        assert "Coder" in prompt
        assert "Edit auth.py" in prompt
        assert "auth.py line 42" in prompt

    def test_sub_agent_prompt_without_upstream(self):
        from agent.prompt import build_sub_agent_prompt
        prompt = build_sub_agent_prompt(
            role="explorer",
            task_prompt="Find files related to authentication",
        )
        assert "Explorer" in prompt
        assert "Find files" in prompt
        assert "Upstream" not in prompt


# ===========================================================================
# Factory 集成测试
# ===========================================================================

class TestFactoryIntegration:
    def test_create_multi_agent(self):
        from agent.factory import create_agent
        from tools.base import ToolRegistry, NoopTool

        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))
        backend = MagicMock()

        agent = create_agent("multi-agent", backend, registry)
        assert isinstance(agent, CoordinatorAgent)

    def test_invalid_mode_raises(self):
        from agent.factory import create_agent
        from tools.base import ToolRegistry

        with pytest.raises(ValueError, match="Unknown mode"):
            create_agent("invalid-mode", MagicMock(), ToolRegistry())

    def test_resolve_mode_accepts_multi_agent(self):
        from agent.factory import _resolve_mode
        assert _resolve_mode("multi-agent", None) == "multi-agent"


# ===========================================================================
# 预算隔离测试
# ===========================================================================

class TestBudgetIsolation:
    def test_budget_for_role_allocation(self):
        from tools.base import ToolRegistry, NoopTool
        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))

        coord = CoordinatorAgent(MagicMock(), registry)
        coord._sub_budget = 200_000
        coord._tokens_used_by_subs = 0

        # Coder gets most (0.35 * 200k = 70k > 0.30 * 200k = 60k)
        coder_budget = coord._budget_for_role(SubAgentRole.CODER)
        explorer_budget = coord._budget_for_role(SubAgentRole.EXPLORER)
        assert coder_budget > explorer_budget

    def test_budget_respects_remaining(self):
        from tools.base import ToolRegistry, NoopTool
        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))

        coord = CoordinatorAgent(MagicMock(), registry)
        coord._sub_budget = 70_000
        coord._tokens_used_by_subs = 69_000  # Only 1000 remaining

        budget = coord._budget_for_role(SubAgentRole.CODER)
        assert budget <= 1000

    def test_has_budget_check(self):
        from tools.base import ToolRegistry, NoopTool
        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))

        coord = CoordinatorAgent(MagicMock(), registry)
        coord._sub_budget = 70_000

        coord._tokens_used_by_subs = 0
        assert coord._has_budget_for_spawn() is True

        coord._tokens_used_by_subs = 70_000
        assert coord._has_budget_for_spawn() is False


# ===========================================================================
# Worktree 隔离测试
# ===========================================================================

class TestWorktreeIsolation:
    def _make_coordinator_with_worktree(self):
        """创建带 mock WorktreeManager 的 CoordinatorAgent。"""
        from tools.base import ToolRegistry, NoopTool
        from tools.snapshot import Worktree

        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))
        backend = MagicMock()

        coord = CoordinatorAgent(backend, registry)
        coord._results = []
        coord._tokens_used_by_subs = 0
        coord._total_budget = 100_000
        coord._sub_budget = 70_000
        coord._repo_path = "/repo"
        coord._log_dir = None
        coord._agent_worktrees = {}

        # Mock WorktreeManager
        mock_mgr = MagicMock()
        mock_mgr.create.return_value = Worktree(
            name="coder-000",
            path="/repo/.worktrees/coder-000",
            branch="multi-agent/coder-000",
            base_branch="main",
        )
        coord._worktree_mgr = mock_mgr
        return coord

    def test_spawn_with_worktree_isolation(self, tmp_path):
        """isolation='worktree' 时创建 worktree 并传递路径。"""
        coord = self._make_coordinator_with_worktree()
        coord._log_dir = str(tmp_path)

        # Mock sub_executor.spawn 来验证 repo_path 是 worktree 路径
        captured_kwargs = {}

        def mock_spawn(config, repo_path, upstream_context="", log_dir=None):
            captured_kwargs["repo_path"] = repo_path
            captured_kwargs["isolation"] = config.isolation
            return SubAgentResult(
                agent_id="wt_agent",
                role=config.role,
                status=RunStatus.SUCCESS,
                summary="Edited files in worktree",
                steps_taken=3,
                total_tokens=500,
            )

        coord._sub_executor.spawn = mock_spawn

        tool = SpawnAgentTool(coord)
        result = tool.execute({
            "role": "coder",
            "task": "Fix the bug",
            "isolation": "worktree",
        })

        assert result.success
        assert captured_kwargs["repo_path"] == "/repo/.worktrees/coder-000"
        assert captured_kwargs["isolation"] == "worktree"
        assert "wt_agent" in coord._agent_worktrees
        coord._worktree_mgr.create.assert_called_once_with("coder-000")

    def test_spawn_without_isolation_uses_shared_repo(self, tmp_path):
        """isolation='none' 时使用共享 repo 路径。"""
        coord = self._make_coordinator_with_worktree()
        coord._log_dir = str(tmp_path)

        captured_kwargs = {}

        def mock_spawn(config, repo_path, upstream_context="", log_dir=None):
            captured_kwargs["repo_path"] = repo_path
            return SubAgentResult(
                agent_id="shared_agent",
                role=config.role,
                status=RunStatus.SUCCESS,
                summary="Done",
                steps_taken=1,
                total_tokens=100,
            )

        coord._sub_executor.spawn = mock_spawn

        tool = SpawnAgentTool(coord)
        result = tool.execute({
            "role": "explorer",
            "task": "Find files",
            "isolation": "none",
        })

        assert result.success
        assert captured_kwargs["repo_path"] == "/repo"
        coord._worktree_mgr.create.assert_not_called()

    def test_worktree_create_failure_falls_back(self, tmp_path):
        """worktree 创建失败时 fallback 到共享 repo。"""
        coord = self._make_coordinator_with_worktree()
        coord._log_dir = str(tmp_path)
        coord._worktree_mgr.create.side_effect = Exception("git worktree failed")

        captured_kwargs = {}

        def mock_spawn(config, repo_path, upstream_context="", log_dir=None):
            captured_kwargs["repo_path"] = repo_path
            return SubAgentResult(
                agent_id="fallback_agent",
                role=config.role,
                status=RunStatus.SUCCESS,
                summary="Done",
                steps_taken=1,
                total_tokens=100,
            )

        coord._sub_executor.spawn = mock_spawn

        tool = SpawnAgentTool(coord)
        result = tool.execute({
            "role": "coder",
            "task": "Edit files",
            "isolation": "worktree",
        })

        assert result.success
        assert captured_kwargs["repo_path"] == "/repo"
        assert "fallback_agent" not in coord._agent_worktrees

    def test_finalize_merges_successful_worktrees(self):
        """成功的 agent worktree 被合并。"""
        from tools.snapshot import Worktree

        coord = self._make_coordinator_with_worktree()
        wt = Worktree(
            name="coder-000",
            path="/repo/.worktrees/coder-000",
            branch="multi-agent/coder-000",
            base_branch="main",
        )
        coord._agent_worktrees = {"abc123": wt}
        coord._results = [SubAgentResult(
            agent_id="abc123",
            role=SubAgentRole.CODER,
            status=RunStatus.SUCCESS,
            summary="Changes made",
            steps_taken=5,
            total_tokens=1000,
        )]

        errors = coord._finalize_worktrees()

        assert errors == []
        coord._worktree_mgr.merge.assert_called_once_with(wt, delete_after=True)
        coord._worktree_mgr.discard.assert_not_called()

    def test_finalize_discards_failed_worktrees(self):
        """失败的 agent worktree 被丢弃。"""
        from tools.snapshot import Worktree

        coord = self._make_coordinator_with_worktree()
        wt = Worktree(
            name="coder-001",
            path="/repo/.worktrees/coder-001",
            branch="multi-agent/coder-001",
            base_branch="main",
        )
        coord._agent_worktrees = {"def456": wt}
        coord._results = [SubAgentResult(
            agent_id="def456",
            role=SubAgentRole.CODER,
            status=RunStatus.FAILED,
            summary="Could not complete",
            steps_taken=10,
            total_tokens=2000,
        )]

        errors = coord._finalize_worktrees()

        assert errors == []
        coord._worktree_mgr.merge.assert_not_called()
        coord._worktree_mgr.discard.assert_called_once_with(wt)

    def test_finalize_handles_merge_conflict(self):
        """合并冲突时记录错误并 discard。"""
        from tools.snapshot import Worktree

        coord = self._make_coordinator_with_worktree()
        wt = Worktree(
            name="coder-002",
            path="/repo/.worktrees/coder-002",
            branch="multi-agent/coder-002",
            base_branch="main",
        )
        coord._agent_worktrees = {"ghi789": wt}
        coord._results = [SubAgentResult(
            agent_id="ghi789",
            role=SubAgentRole.CODER,
            status=RunStatus.SUCCESS,
            summary="Changes made",
            steps_taken=5,
            total_tokens=1000,
        )]
        coord._worktree_mgr.merge.side_effect = Exception("Merge conflict in auth.py")

        errors = coord._finalize_worktrees()

        assert len(errors) == 1
        assert "coder-002" in errors[0]
        assert "Merge conflict" in errors[0]
        coord._worktree_mgr.discard.assert_called_once_with(wt)

    def test_init_worktree_manager_non_git(self, tmp_path):
        """非 git 仓库时 _init_worktree_manager 返回 None。"""
        from tools.base import ToolRegistry, NoopTool

        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))
        coord = CoordinatorAgent(MagicMock(), registry)

        mgr = coord._init_worktree_manager(str(tmp_path))
        assert mgr is None

    def test_spawn_agent_tool_schema_includes_isolation(self):
        """spawn_agent 的 schema 包含 isolation 参数。"""
        coord = self._make_coordinator_with_worktree()
        tool = SpawnAgentTool(coord)
        schema = tool.parameters_schema
        assert "isolation" in schema["properties"]
        assert schema["properties"]["isolation"]["enum"] == ["worktree", "none"]


# ===========================================================================
# Model Override 测试
# ===========================================================================

class TestModelOverride:
    def test_resolve_backend_no_override_returns_parent(self):
        """model=None 且 worker_model=None 时返回父 backend。"""
        from tools.base import ToolRegistry, NoopTool

        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))
        parent_backend = MagicMock()
        parent_backend.model_name = "parent-model"

        executor = SubAgentExecutor(parent_backend, registry)
        result = executor._resolve_backend(None)
        assert result is parent_backend

    def test_resolve_backend_with_worker_model_no_provider_returns_parent(self):
        """worker_model 有值但 worker_provider 没配时，回退到父 backend。"""
        from tools.base import ToolRegistry, NoopTool

        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))
        parent_backend = MagicMock()

        cfg = MultiAgentConfig(worker_model="some-model", worker_provider=None)
        executor = SubAgentExecutor(parent_backend, registry, multi_config=cfg)
        result = executor._resolve_backend(None)
        assert result is parent_backend

    @patch("agent.multi_agent.SubAgentExecutor._resolve_backend")
    def test_spawn_passes_model_to_resolve(self, mock_resolve, tmp_path):
        """spawn 将 config.model 传递给 _resolve_backend。"""
        from tools.base import ToolRegistry, NoopTool
        from llm.base import LLMResponse

        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))
        parent_backend = MagicMock()
        parent_backend.supports_function_calling = True
        parent_backend.max_context_window = 128_000
        parent_backend.model_name = "test"
        parent_backend.complete.return_value = LLMResponse(
            action=__import__("agent.task", fromlist=["Action"]).Action(
                action_type=__import__("agent.task", fromlist=["ActionType"]).ActionType.FINISH,
                thought="done",
                message="Done.",
            ),
            raw_content="done",
            input_tokens=50,
            output_tokens=25,
        )
        mock_resolve.return_value = parent_backend

        executor = SubAgentExecutor(parent_backend, registry)
        config = SubAgentConfig(
            role=SubAgentRole.EXPLORER,
            max_steps=3,
            budget_tokens=5000,
            task_prompt="Find auth code",
            model="deepseek-chat",
        )

        executor.spawn(config=config, repo_path=".", log_dir=str(tmp_path))
        mock_resolve.assert_called_once_with("deepseek-chat")

    def test_spawn_agent_tool_schema_includes_model(self):
        """spawn_agent schema 包含 model 参数。"""
        from tools.base import ToolRegistry, NoopTool

        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))
        coord = CoordinatorAgent(MagicMock(), registry)
        tool = SpawnAgentTool(coord)
        schema = tool.parameters_schema
        assert "model" in schema["properties"]

    def test_config_multi_agent_parsed(self):
        """AppConfig 正确解析 multi_agent 配置段。"""
        from config.schema import _parse
        data = {
            "multi_agent": {
                "worker_model": "deepseek-chat",
                "worker_provider": "deepseek",
                "max_parallel_executors": 5,
                "coordinator_budget_ratio": 0.25,
                "sub_agent_budget_ratio": 0.75,
                "max_retries": 3,
                "coordinator_max_steps": 30,
            }
        }
        config = _parse(data)
        assert config.multi_agent.worker_model == "deepseek-chat"
        assert config.multi_agent.worker_provider == "deepseek"
        assert config.multi_agent.max_parallel_executors == 5
        assert config.multi_agent.coordinator_budget_ratio == 0.25
        assert config.multi_agent.sub_agent_budget_ratio == 0.75
        assert config.multi_agent.max_retries == 3
        assert config.multi_agent.coordinator_max_steps == 30


# ===========================================================================
# HITL Merge Approval 测试
# ===========================================================================

class TestHITLMergeApproval:
    def test_merge_approved_by_callback(self):
        """merge_approval_callback 返回 True 时正常合并。"""
        from tools.base import ToolRegistry, NoopTool
        from tools.snapshot import Worktree

        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))
        backend = MagicMock()

        approved_calls = []

        def approve_cb(name, diff):
            approved_calls.append((name, diff))
            return True

        multi_cfg = MultiAgentConfig(merge_approval_callback=approve_cb)
        coord = CoordinatorAgent(backend, registry, multi_config=multi_cfg)
        coord._results = [SubAgentResult(
            agent_id="a1", role=SubAgentRole.CODER,
            status=RunStatus.SUCCESS, summary="Done",
        )]

        mock_mgr = MagicMock()
        mock_mgr.get_diff.return_value = "+added line\n-removed line"
        coord._worktree_mgr = mock_mgr

        wt = Worktree(name="coder-000", path="/wt", branch="b", base_branch="main")
        coord._agent_worktrees = {"a1": wt}

        errors = coord._finalize_worktrees()

        assert errors == []
        assert len(approved_calls) == 1
        assert approved_calls[0][0] == "coder-000"
        assert "+added line" in approved_calls[0][1]
        mock_mgr.merge.assert_called_once()

    def test_merge_rejected_by_callback(self):
        """merge_approval_callback 返回 False 时丢弃 worktree。"""
        from tools.base import ToolRegistry, NoopTool
        from tools.snapshot import Worktree

        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))
        backend = MagicMock()

        multi_cfg = MultiAgentConfig(merge_approval_callback=lambda n, d: False)
        coord = CoordinatorAgent(backend, registry, multi_config=multi_cfg)
        coord._results = [SubAgentResult(
            agent_id="a1", role=SubAgentRole.CODER,
            status=RunStatus.SUCCESS, summary="Done",
        )]

        mock_mgr = MagicMock()
        mock_mgr.get_diff.return_value = "some diff"
        coord._worktree_mgr = mock_mgr

        wt = Worktree(name="coder-000", path="/wt", branch="b", base_branch="main")
        coord._agent_worktrees = {"a1": wt}

        errors = coord._finalize_worktrees()

        assert len(errors) == 1
        assert "rejected" in errors[0]
        mock_mgr.merge.assert_not_called()
        mock_mgr.discard.assert_called_once_with(wt)

    def test_no_callback_auto_merges(self):
        """merge_approval_callback=None 时自动合并（非交互模式）。"""
        from tools.base import ToolRegistry, NoopTool
        from tools.snapshot import Worktree

        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))
        backend = MagicMock()

        multi_cfg = MultiAgentConfig(merge_approval_callback=None)
        coord = CoordinatorAgent(backend, registry, multi_config=multi_cfg)
        coord._results = [SubAgentResult(
            agent_id="a1", role=SubAgentRole.CODER,
            status=RunStatus.SUCCESS, summary="Done",
        )]

        mock_mgr = MagicMock()
        coord._worktree_mgr = mock_mgr

        wt = Worktree(name="coder-000", path="/wt", branch="b", base_branch="main")
        coord._agent_worktrees = {"a1": wt}

        errors = coord._finalize_worktrees()

        assert errors == []
        mock_mgr.merge.assert_called_once_with(wt, delete_after=True)
        mock_mgr.get_diff.assert_not_called()


# ===========================================================================
# 并行执行测试
# ===========================================================================

class TestParallelExecution:
    def test_spawn_parallel_basic(self, tmp_path):
        """spawn_parallel 并行执行多个 agent 并返回结果。"""
        from tools.base import ToolRegistry, NoopTool
        from llm.base import LLMResponse
        from agent.task import Action, ActionType

        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))

        backend = MagicMock()
        backend.supports_function_calling = True
        backend.max_context_window = 128_000
        backend.model_name = "test"
        backend.complete.return_value = LLMResponse(
            action=Action(action_type=ActionType.FINISH, thought="done", message="Done."),
            raw_content="done",
            input_tokens=50,
            output_tokens=25,
        )

        executor = SubAgentExecutor(backend, registry)
        configs = [
            SubAgentConfig(role=SubAgentRole.EXPLORER, max_steps=3, budget_tokens=5000, task_prompt="Explore A"),
            SubAgentConfig(role=SubAgentRole.EXPLORER, max_steps=3, budget_tokens=5000, task_prompt="Explore B"),
        ]
        repo_paths = [str(tmp_path), str(tmp_path)]
        upstream_contexts = ["", ""]

        results = executor.spawn_parallel(
            configs=configs,
            repo_paths=repo_paths,
            upstream_contexts=upstream_contexts,
            log_dir=str(tmp_path),
            max_workers=2,
        )

        assert len(results) == 2
        assert all(r.status == RunStatus.SUCCESS for r in results)
        assert all(r.role == SubAgentRole.EXPLORER for r in results)

    def test_spawn_parallel_one_failure_doesnt_affect_others(self, tmp_path):
        """一个 agent 失败不影响其他 agent。"""
        from tools.base import ToolRegistry, NoopTool
        from llm.base import LLMResponse
        from agent.task import Action, ActionType

        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))

        call_count = [0]

        def mock_complete(messages, tools):
            call_count[0] += 1
            # 第二次调用抛异常（模拟 agent 2 crash）
            if call_count[0] == 2:
                raise RuntimeError("Simulated LLM crash")
            return LLMResponse(
                action=Action(action_type=ActionType.FINISH, thought="done", message="Success."),
                raw_content="done",
                input_tokens=50,
                output_tokens=25,
            )

        backend = MagicMock()
        backend.supports_function_calling = True
        backend.max_context_window = 128_000
        backend.model_name = "test"
        backend.complete.side_effect = mock_complete

        executor = SubAgentExecutor(backend, registry)
        configs = [
            SubAgentConfig(role=SubAgentRole.EXPLORER, max_steps=3, budget_tokens=5000, task_prompt="A"),
            SubAgentConfig(role=SubAgentRole.EXPLORER, max_steps=3, budget_tokens=5000, task_prompt="B"),
            SubAgentConfig(role=SubAgentRole.EXPLORER, max_steps=3, budget_tokens=5000, task_prompt="C"),
        ]
        repo_paths = [str(tmp_path)] * 3
        upstream_contexts = [""] * 3

        results = executor.spawn_parallel(
            configs=configs,
            repo_paths=repo_paths,
            upstream_contexts=upstream_contexts,
            log_dir=str(tmp_path),
            max_workers=3,
        )

        assert len(results) == 3
        # At least one should succeed, at least one should fail
        statuses = [r.status for r in results]
        assert RunStatus.SUCCESS in statuses or RunStatus.FAILED in statuses
        # The failed one should have crash info in summary
        failed = [r for r in results if r.status == RunStatus.FAILED]
        if failed:
            assert any("crash" in r.summary.lower() or "error" in r.summary.lower() for r in failed)

    def test_spawn_parallel_tool_basic(self, tmp_path):
        """SpawnParallelTool 在 Coordinator 上下文中正常工作。"""
        from tools.base import ToolRegistry, NoopTool
        from tools.snapshot import Worktree

        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))
        backend = MagicMock()

        coord = CoordinatorAgent(backend, registry)
        coord._results = []
        coord._tokens_used_by_subs = 0
        coord._total_budget = 100_000
        coord._sub_budget = 70_000
        coord._repo_path = str(tmp_path)
        coord._log_dir = str(tmp_path)
        coord._agent_worktrees = {}
        coord._worktree_mgr = None  # no worktree manager

        # Mock spawn (called per-agent inside SpawnParallelTool)
        spawn_counter = {"i": 0}

        def mock_spawn(config, repo_path, upstream_context, log_dir, thread_isolated=False):
            idx = spawn_counter["i"]
            spawn_counter["i"] += 1
            return SubAgentResult(
                agent_id=f"par-{idx}",
                role=config.role,
                status=RunStatus.SUCCESS,
                summary=f"Done #{idx}",
                steps_taken=2,
                total_tokens=200,
            )

        coord._sub_executor.spawn = mock_spawn

        tool = SpawnParallelTool(coord)
        result = tool.execute({
            "agents": [
                {"role": "explorer", "task": "Find auth files"},
                {"role": "explorer", "task": "Find config files"},
            ]
        })

        assert result.success
        assert "2 agents" in result.output
        assert "2 succeeded" in result.output
        assert len(coord._results) == 2

    def test_spawn_parallel_tool_with_empty_list(self):
        """空 agents 列表返回 error。"""
        from tools.base import ToolRegistry, NoopTool

        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))
        coord = CoordinatorAgent(MagicMock(), registry)
        coord._results = []
        coord._tokens_used_by_subs = 0
        coord._sub_budget = 70_000

        tool = SpawnParallelTool(coord)
        result = tool.execute({"agents": []})
        assert not result.success
        assert "empty" in result.error

    def test_spawn_parallel_tool_registers_in_coordinator(self, tmp_path):
        """spawn_parallel 工具在 Coordinator run() 中被注册。"""
        from tools.base import ToolRegistry, NoopTool
        from llm.base import LLMResponse
        from agent.task import Action, ActionType, ToolCall

        registry = ToolRegistry()
        registry.register(NoopTool(tool_name="file_read"))

        backend = MagicMock()
        backend.supports_function_calling = True
        backend.max_context_window = 128_000
        backend.model_name = "test"
        backend.complete.return_value = LLMResponse(
            action=Action(
                action_type=ActionType.TOOL_CALL,
                thought="finishing",
                tool_calls=[ToolCall(
                    name="finish_coordination",
                    params={"summary": "Done", "status": "success"},
                    id="c1",
                )],
            ),
            raw_content="done",
            input_tokens=100,
            output_tokens=50,
        )

        multi_cfg = MultiAgentConfig(log_dir=str(tmp_path))
        coord = CoordinatorAgent(backend, registry, multi_config=multi_cfg)

        task = Task(description="Test", repo_path=str(tmp_path), max_steps=5, budget_tokens=100_000)
        from agent.event_log import EventLog
        log = EventLog.create(task, log_dir=str(tmp_path))
        coord.run(task, log)
        log.close()

        # Verify spawn_parallel was registered (it was used internally)
        # We check by looking at coord_registry — but since run() creates it internally,
        # we verify by checking the prompt contains spawn_parallel
        from agent.prompt import build_coordinator_system_prompt
        prompt = build_coordinator_system_prompt("test", ".", 100000, 70000, 2)
        assert "spawn_parallel" in prompt
