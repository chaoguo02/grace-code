"""
agent/multi_agent.py

Multi-Agent 协作系统。

架构：
- CoordinatorAgent: LLM 驱动的调度器（本身是 ReActAgent，通过 tool 调用 spawn 子 Agent）
- SubAgentExecutor: 子 Agent 执行器，创建隔离的 ReActAgent 实例
- SubAgentRole: 角色枚举（explorer / planner / coder / reviewer / tester）

设计：
- Coordinator 是 LLM 驱动的 ReActAgent，拥有 spawn_agent / list_agent_results 工具
- LLM 自行决策：何时 spawn、spawn 哪个角色、何时重试、何时结束
- 每个子 Agent 有独立的 ConversationHistory + 过滤后的 ToolRegistry + 角色 prompt
- 上下文通过 result_summary 摘要在 Agent 间传递（不共享原始对话）
- Token 预算隔离：Coordinator 30% / SubAgents 70%

通信机制：
- 单向数据流：Coordinator → SubAgent → 结果回传
- SubAgent 间不直接通信，通过 Coordinator 中转
- 结果以 SubAgentResult 结构化存储，Coordinator 可按角色查询
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TYPE_CHECKING

from agent.event_log import EventLog
from agent.task import RunResult, RunStatus, Task
from context.history import ConversationHistory
from llm.base import LLMMessage

if TYPE_CHECKING:
    from agent.core import AgentConfig
    from llm.base import LLMBackend
    from memory.context import MemoryContext
    from tools.base import ToolRegistry
    from tools.snapshot import Worktree, WorktreeManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SubAgent 角色
# ---------------------------------------------------------------------------

class SubAgentRole(str, Enum):
    """子 Agent 角色。每个角色有独立的工具权限和 prompt。"""
    EXPLORER = "explorer"
    PLANNER = "planner"
    CODER = "coder"
    REVIEWER = "reviewer"
    TESTER = "tester"


# 每个角色可用的工具白名单
ROLE_TOOL_WHITELIST: dict[SubAgentRole, frozenset[str]] = {
    SubAgentRole.EXPLORER: frozenset({
        "file_read", "file_view", "find_files", "find_symbol",
        "search_text", "git_status", "git_diff",
        "web_search", "web_fetch",
    }),
    SubAgentRole.PLANNER: frozenset({
        "file_read", "file_view", "find_files", "find_symbol",
        "search_text", "git_status", "git_diff",
        "web_search", "web_fetch",
    }),
    SubAgentRole.CODER: frozenset({
        "file_read", "file_view", "file_write", "find_files",
        "find_symbol", "search_text", "shell",
        "git_status", "git_diff", "git_add", "git_commit",
        "web_search", "web_fetch",
    }),
    SubAgentRole.REVIEWER: frozenset({
        "file_read", "file_view", "find_files", "find_symbol",
        "search_text", "git_status", "git_diff",
        "shell", "pytest",
    }),
    SubAgentRole.TESTER: frozenset({
        "file_read", "file_view", "find_files", "search_text",
        "shell", "pytest", "git_status", "git_diff",
    }),
}


# ---------------------------------------------------------------------------
# SubAgent 配置与结果
# ---------------------------------------------------------------------------

@dataclass
class SubAgentConfig:
    """单个子 Agent 的运行配置。"""
    role: SubAgentRole
    max_steps: int = 15
    budget_tokens: int = 30_000
    task_prompt: str = ""
    depends_on: list[str] = field(default_factory=list)
    isolation: str | None = None  # "worktree" 或 None
    model: str | None = None  # 覆盖模型（None = 使用 Coordinator 同款）


@dataclass
class SubAgentResult:
    """子 Agent 的执行结果。"""
    agent_id: str
    role: SubAgentRole
    status: RunStatus
    summary: str
    steps_taken: int = 0
    total_tokens: int = 0
    patch: str | None = None

    def to_display(self) -> str:
        """格式化为 Coordinator 可读的摘要。"""
        status_str = "✓" if self.status == RunStatus.SUCCESS else "✗"
        return (
            f"[{status_str} {self.role.value}#{self.agent_id}] "
            f"({self.steps_taken} steps, {self.total_tokens} tokens)\n"
            f"{self.summary}"
        )


# ---------------------------------------------------------------------------
# SubAgentExecutor — 子 Agent 执行器
# ---------------------------------------------------------------------------

class SubAgentExecutor:
    """
    创建并执行一个角色隔离的 ReActAgent。

    隔离措施：
    - 独立的 ConversationHistory（不共享 Coordinator 历史）
    - 过滤后的 ToolRegistry（只含角色允许的工具）
    - 角色专用的 system prompt 注入
    - 独立的 token 预算
    - 可选的模型覆盖（轻量模型用于 explorer/planner）
    """

    def __init__(
        self,
        backend: "LLMBackend",
        full_registry: "ToolRegistry",
        parent_config: "AgentConfig | None" = None,
        memory_context: "MemoryContext | None" = None,
        multi_config: MultiAgentConfig | None = None,
    ) -> None:
        self._backend = backend
        self._full_registry = full_registry
        self._parent_config = parent_config
        self._memory_context = memory_context
        self._multi_cfg = multi_config or MultiAgentConfig()
        self._worker_backend_cache: dict[str, "LLMBackend"] = {}

    def spawn(
        self,
        config: SubAgentConfig,
        repo_path: str,
        upstream_context: str = "",
        log_dir: str | None = None,
        thread_isolated: bool = False,
    ) -> SubAgentResult:
        """
        Spawn 一个子 Agent 并执行。

        Args:
            config:           子 Agent 配置
            repo_path:        代码仓库路径
            upstream_context: 上游 Agent 的结果摘要（注入 prompt）
            log_dir:          日志目录
            thread_isolated:  是否在子线程中运行（禁用 SQLite-backed memory）

        Returns:
            SubAgentResult
        """
        from agent.core import AgentConfig, ReActAgent
        from agent.prompt import build_sub_agent_prompt

        # 构建角色受限的 ToolRegistry
        filtered_registry = self._filter_registry(config.role)

        # 解析 backend（支持模型覆盖）
        backend = self._resolve_backend(config.model)

        # 构建 AgentConfig（子线程禁用流式回调，避免并发写 stdout）
        agent_cfg = AgentConfig(
            max_steps=config.max_steps,
            budget_tokens=config.budget_tokens,
            history_max_messages=30,
            compact_history=False,  # 子 agent 短生命周期，禁用积极压缩保留完整上下文
            llm_max_retries=self._parent_config.llm_max_retries if self._parent_config else 3,
            llm_retry_delay=self._parent_config.llm_retry_delay if self._parent_config else 2.0,
            stream=False if thread_isolated else (self._parent_config.stream if self._parent_config else False),
            stream_callback=None if thread_isolated else (self._parent_config.stream_callback if self._parent_config else None),
            thought_callback=None if thread_isolated else (self._parent_config.thought_callback if self._parent_config else None),
            confirm_dangerous=self._parent_config.confirm_dangerous if self._parent_config else False,
            confirm_callback=self._parent_config.confirm_callback if self._parent_config else None,
        )

        # 创建独立的 ReActAgent（子线程不传 memory_context，SQLite 不线程安全）
        memory_ctx = None if thread_isolated else self._memory_context
        agent = ReActAgent(
            backend, filtered_registry, agent_cfg,
            memory_context=memory_ctx,
        )

        # 只读角色：切换到 plan mode
        if config.role in (SubAgentRole.EXPLORER, SubAgentRole.PLANNER):
            agent.switch_to_plan_mode()

        # 构建任务 prompt
        full_prompt = build_sub_agent_prompt(
            role=config.role.value,
            task_prompt=config.task_prompt,
            upstream_context=upstream_context,
        )

        # 注入独立 history
        history = ConversationHistory(max_messages=30)
        history.add(LLMMessage(role="user", content=full_prompt))
        agent._pending_history = history

        # 创建 Task 并执行
        sub_task = Task(
            description=full_prompt,
            repo_path=repo_path,
            max_steps=config.max_steps,
            budget_tokens=config.budget_tokens,
        )

        sub_log = EventLog.create(sub_task, log_dir=log_dir)
        result = agent.run(sub_task, sub_log)
        sub_log.close()

        return SubAgentResult(
            agent_id=sub_task.task_id[:8],
            role=config.role,
            status=result.status,
            summary=result.summary or "",
            steps_taken=result.steps_taken,
            total_tokens=result.total_tokens,
            patch=result.patch,
        )

    def _resolve_backend(self, model_override: str | None) -> "LLMBackend":
        """
        解析子 Agent 应使用的 LLM backend。

        优先级：config.model > multi_cfg.worker_model > 父 backend（同款）

        使用缓存避免为同一模型重复创建 backend。
        要使用不同模型，必须同时配置 worker_provider。
        """
        target_model = model_override or self._multi_cfg.worker_model
        if not target_model:
            return self._backend

        # 必须有 provider 才能创建新 backend
        provider = self._multi_cfg.worker_provider
        if not provider:
            logger.debug("worker_model=%s but no worker_provider set, using parent backend", target_model)
            return self._backend

        # 缓存 key: provider/model
        cache_key = f"{provider}/{target_model}"
        if cache_key in self._worker_backend_cache:
            return self._worker_backend_cache[cache_key]

        # 创建新 backend
        try:
            from llm.router import create_backend
            backend = create_backend(
                provider=provider,
                model=target_model,
            )
            self._worker_backend_cache[cache_key] = backend
            logger.info("Created worker backend: provider=%s, model=%s", provider, target_model)
            return backend
        except Exception as e:
            logger.warning("Failed to create worker backend (%s/%s): %s. Using parent backend.", provider, target_model, e)
            return self._backend

    def spawn_parallel(
        self,
        configs: list[SubAgentConfig],
        repo_paths: list[str],
        upstream_contexts: list[str],
        log_dir: str | None = None,
        max_workers: int = 3,
        timeout_per_agent: float = 300.0,
    ) -> list[SubAgentResult]:
        """
        并行 Spawn 多个子 Agent（线程池隔离）。

        每个子 Agent 在独立线程中运行，异常不会传播到其他线程。
        使用 ThreadPoolExecutor 是安全的，因为：
        - 每个 Agent 有独立的 ConversationHistory（无共享可变状态）
        - 工具通过 cwd= 参数操作指定目录（不依赖进程 CWD）
        - LLMBackend 的 HTTP 调用是线程安全的

        Args:
            configs:           各子 Agent 配置列表
            repo_paths:        各子 Agent 的工作目录（worktree path）
            upstream_contexts: 各子 Agent 的上游上下文
            log_dir:           日志目录
            max_workers:       最大并行数
            timeout_per_agent: 每个 Agent 的超时（秒）

        Returns:
            SubAgentResult 列表（与 configs 对应，失败的返回 FAILED 状态）
        """
        results: list[SubAgentResult | None] = [None] * len(configs)

        def _run_one(idx: int) -> SubAgentResult:
            config = configs[idx]
            repo_path = repo_paths[idx]
            upstream = upstream_contexts[idx] if idx < len(upstream_contexts) else ""
            return self.spawn(
                config=config,
                repo_path=repo_path,
                upstream_context=upstream,
                log_dir=log_dir,
            )

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="sub-agent") as pool:
            future_to_idx: dict[Future, int] = {}
            for i in range(len(configs)):
                fut = pool.submit(_run_one, i)
                future_to_idx[fut] = i

            for fut in as_completed(future_to_idx):
                idx = future_to_idx[fut]
                config = configs[idx]
                try:
                    results[idx] = fut.result(timeout=timeout_per_agent)
                except TimeoutError:
                    logger.error("Sub-agent %s timed out after %.0fs", config.role.value, timeout_per_agent)
                    results[idx] = SubAgentResult(
                        agent_id=f"timeout-{idx:03d}",
                        role=config.role,
                        status=RunStatus.FAILED,
                        summary=f"Agent timed out after {timeout_per_agent}s",
                        steps_taken=0,
                        total_tokens=0,
                    )
                except Exception as e:
                    logger.error("Sub-agent %s crashed: %s", config.role.value, e, exc_info=True)
                    results[idx] = SubAgentResult(
                        agent_id=f"error-{idx:03d}",
                        role=config.role,
                        status=RunStatus.FAILED,
                        summary=f"Agent crashed: {type(e).__name__}: {e}",
                        steps_taken=0,
                        total_tokens=0,
                    )

        return [r for r in results if r is not None]

    def _filter_registry(self, role: SubAgentRole) -> "ToolRegistry":
        """根据角色白名单过滤工具注册表。"""
        from tools.base import ToolRegistry

        allowed = ROLE_TOOL_WHITELIST.get(role, frozenset())
        filtered = ToolRegistry()

        for tool_name in self._full_registry.tool_names:
            if tool_name in allowed:
                tool = self._full_registry._tools[tool_name]
                filtered._tools[tool_name] = tool

        return filtered


# ---------------------------------------------------------------------------
# Coordinator 专用工具
# ---------------------------------------------------------------------------

from tools.base import BaseTool, ToolResult


class SpawnAgentTool(BaseTool):
    """
    Coordinator 专用工具：Spawn 一个子 Agent。

    LLM 调用此工具来创建一个特定角色的子 Agent，
    子 Agent 在独立上下文中执行，结果以摘要形式返回。
    """

    def __init__(self, coordinator: "CoordinatorAgent") -> None:
        self._coordinator = coordinator

    @property
    def name(self) -> str:
        return "spawn_agent"

    @property
    def description(self) -> str:
        return (
            "Spawn a sub-agent with a specific role to perform a task. "
            "Roles: explorer (read-only code search), planner (create execution plan), "
            "coder (edit files), reviewer (review changes + run tests), tester (run tests only). "
            "The sub-agent runs in an isolated context and returns a summary of its work."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "enum": ["explorer", "planner", "coder", "reviewer", "tester"],
                    "description": "The role/specialization of the sub-agent",
                },
                "task": {
                    "type": "string",
                    "description": "Clear, specific instructions for the sub-agent. Include file paths, function names, and expected outcomes.",
                },
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Agent IDs whose results should be injected as upstream context (optional)",
                },
                "isolation": {
                    "type": "string",
                    "enum": ["worktree", "none"],
                    "description": "Isolation mode. Use 'worktree' for coder agents that may edit files in parallel. Default: none.",
                },
                "model": {
                    "type": "string",
                    "description": "Override model for this sub-agent (e.g. a lighter model for exploration). Omit to use default.",
                },
            },
            "required": ["role", "task"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        role_str = params.get("role", "")
        task_prompt = params.get("task", "")
        depends_on = params.get("depends_on", [])
        isolation = params.get("isolation", "none")
        model = params.get("model") or None

        if not task_prompt:
            return ToolResult(success=False, output="", error="'task' is required")

        try:
            role = SubAgentRole(role_str)
        except ValueError:
            return ToolResult(
                success=False, output="",
                error=f"Unknown role '{role_str}'. Valid: explorer, planner, coder, reviewer, tester",
            )

        # 检查预算
        if not self._coordinator._has_budget_for_spawn():
            return ToolResult(
                success=False, output="",
                error="Sub-agent token budget exhausted. Finish coordination or reduce scope.",
            )

        # 检查同一 role 的 spawn 次数（防止 coordinator 对失败结果无限重试）
        count = self._coordinator._role_spawn_counts.get(role_str, 0)
        max_per_role = 2
        if count >= max_per_role:
            return ToolResult(
                success=False, output="",
                error=f"Already spawned {count} {role_str} agents (max {max_per_role}). "
                      f"Use list_agent_results to see their output and call finish_coordination.",
            )

        # 收集上游上下文
        upstream_context = ""
        if depends_on:
            parts = []
            for agent_id in depends_on:
                r = self._coordinator._get_result(agent_id)
                if r:
                    parts.append(r.to_display())
            upstream_context = "\n\n".join(parts)

        # Worktree 隔离
        worktree = None
        working_dir = self._coordinator._repo_path
        if isolation == "worktree" and self._coordinator._worktree_mgr is not None:
            try:
                wt_name = f"{role_str}-{len(self._coordinator._results):03d}"
                worktree = self._coordinator._worktree_mgr.create(wt_name)
                working_dir = worktree.path
                logger.info("Created worktree '%s' for sub-agent at %s", wt_name, working_dir)
            except Exception as e:
                logger.warning("Failed to create worktree, falling back to shared repo: %s", e)
                worktree = None
                working_dir = self._coordinator._repo_path

        # 执行
        config = SubAgentConfig(
            role=role,
            max_steps=self._coordinator._steps_for_role(role),
            budget_tokens=self._coordinator._budget_for_role(role),
            task_prompt=task_prompt,
            depends_on=depends_on,
            isolation="worktree" if worktree else None,
            model=model,
        )

        result = self._coordinator._sub_executor.spawn(
            config=config,
            repo_path=working_dir,
            upstream_context=upstream_context,
            log_dir=self._coordinator._log_dir,
        )

        # 记录 worktree 映射（供后续 finalize 使用）
        if worktree is not None:
            self._coordinator._agent_worktrees[result.agent_id] = worktree

        self._coordinator._spawn_count += 1
        self._coordinator._results.append(result)

        # 递增 role spawn 计数
        self._coordinator._role_spawn_counts[role_str] = self._coordinator._role_spawn_counts.get(role_str, 0) + 1

        return ToolResult(
            success=True,
            output=f"Agent #{result.agent_id} ({result.role.value}) completed: "
                   f"{result.status.value} in {result.steps_taken} steps, "
                   f"{result.total_tokens} tokens.\n\nSummary:\n{result.summary}",
        )


class ListAgentResultsTool(BaseTool):
    """Coordinator 专用工具：列出所有已完成的子 Agent 结果。"""

    def __init__(self, coordinator: "CoordinatorAgent") -> None:
        self._coordinator = coordinator

    @property
    def name(self) -> str:
        return "list_agent_results"

    @property
    def description(self) -> str:
        return (
            "List all completed sub-agent results. "
            "Use this to check what agents have finished and their outcomes. "
            "Results include role, status, steps taken, tokens used, and a summary."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "enum": ["explorer", "planner", "coder", "reviewer", "tester"],
                    "description": "Filter by role (optional)",
                }
            },
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        role_filter = params.get("role")
        results = self._coordinator._results
        if role_filter and role_filter != "all":
            results = [r for r in results if r.role.value == role_filter]
        if not results:
            return ToolResult(success=True, output="No sub-agent results available.")
        lines = ["Sub-Agent Results:", "---"]
        for result in results:
            lines.append(f"  {result.to_display()}")
        return ToolResult(success=True, output="\n".join(lines))


class FinishCoordinationTool(BaseTool):
    """Coordinator 专用工具：完成协调，返回最终结果。"""

    def __init__(self, coordinator: "CoordinatorAgent") -> None:
        self._coordinator = coordinator

    @property
    def name(self) -> str:
        return "finish_coordination"

    @property
    def description(self) -> str:
        return (
            "Signal that coordination is done. Call this when all sub-agents have "
            "completed, or when the task is done. Provide a summary of what was accomplished."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Final summary of what was accomplished by all sub-agents",
                },
                "status": {
                    "type": "string",
                    "enum": ["success", "partial", "failed"],
                    "description": "Overall coordination outcome",
                },
            },
            "required": ["summary"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        summary = params.get("summary", "Coordination complete.")
        status = params.get("status", "success")
        self._coordinator._final_finished = True
        self._coordinator._final_summary = summary
        self._coordinator._final_status = status
        return ToolResult(
            success=True,
            output=f"Coordination finished: {status}",
        )


class SpawnParallelTool(BaseTool):
    """Coordinator 专用工具：并行 spawn 多个子 Agent。"""

    def __init__(self, coordinator: "CoordinatorAgent") -> None:
        self._coordinator = coordinator

    @property
    def name(self) -> str:
        return "spawn_parallel"

    @property
    def description(self) -> str:
        return (
            "Spawn multiple sub-agents in parallel. Each agent gets its own isolated "
            "context. Use for independent tasks that don't depend on each other. "
            "Each agent spec needs: role, task, and optionally isolation/model."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "agents": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {
                                "type": "string",
                                "enum": ["explorer", "planner", "coder", "reviewer", "tester"],
                            },
                            "task": {"type": "string"},
                            "isolation": {
                                "type": "string",
                                "enum": ["worktree", "none"],
                            },
                            "model": {"type": "string"},
                        },
                        "required": ["role", "task"],
                    },
                    "description": "List of agent specs to spawn in parallel",
                },
            },
            "required": ["agents"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        agents_spec = params.get("agents", [])
        if not agents_spec:
            return ToolResult(success=False, output="", error="'agents' list is empty")

        if not self._coordinator._has_budget_for_spawn():
            return ToolResult(
                success=False, output="",
                error="Sub-agent budget exhausted. Call finish_coordination.",
            )

        results = []

        def _run_one(spec: dict) -> SubAgentResult:
            role_str = spec.get("role", "explorer")
            task_prompt = spec.get("task", "")
            isolation = spec.get("isolation", "none")
            model = spec.get("model") or None

            try:
                role = SubAgentRole(role_str)
            except ValueError:
                return SubAgentResult(
                    agent_id="error", role=SubAgentRole.EXPLORER,
                    status=RunStatus.FAILED,
                    summary=f"Unknown role: {role_str}",
                )

            worktree = None
            working_dir = self._coordinator._repo_path
            if isolation == "worktree" and self._coordinator._worktree_mgr is not None:
                try:
                    wt_name = f"{role_str}-{self._coordinator._spawn_count:03d}"
                    worktree = self._coordinator._worktree_mgr.create(wt_name)
                    working_dir = worktree.path
                except Exception as e:
                    logger.warning("Failed to create worktree: %s", e)
                    worktree = None
                    working_dir = self._coordinator._repo_path

            config = SubAgentConfig(
                role=role,
                max_steps=self._coordinator._steps_for_role(role),
                budget_tokens=self._coordinator._budget_for_role(role),
                task_prompt=task_prompt,
                isolation="worktree" if worktree else None,
                model=model,
            )

            result = self._coordinator._sub_executor.spawn(
                config=config,
                repo_path=working_dir,
                upstream_context="",
                log_dir=self._coordinator._log_dir,
                thread_isolated=True,
            )

            self._coordinator._spawn_count += 1
            self._coordinator._role_spawn_counts[role_str] = self._coordinator._role_spawn_counts.get(role_str, 0) + 1

            if worktree is not None and self._coordinator._worktree_mgr is not None:
                try:
                    if result.status == RunStatus.SUCCESS:
                        self._coordinator._worktree_mgr.merge(worktree)
                    else:
                        self._coordinator._worktree_mgr.discard(worktree)
                except Exception as e:
                    logger.warning("Worktree cleanup failed: %s", e)

            return result

        # 并行执行
        max_workers = min(len(agents_spec), self._coordinator._multi_cfg.max_parallel)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures: list[Future] = []
            for spec in agents_spec:
                futures.append(pool.submit(_run_one, spec))

            for future in as_completed(futures):
                try:
                    result = future.result(timeout=self._coordinator._multi_cfg.timeout_per_agent)
                    results.append(result)
                    self._coordinator._results.append(result)
                except Exception as e:
                    logger.error("Parallel agent failed: %s", e)
                    results.append(SubAgentResult(
                        agent_id="error", role=SubAgentRole.EXPLORER,
                        status=RunStatus.FAILED, summary=f"Exception: {e}",
                    ))

        # 格式化输出
        succeeded = sum(1 for r in results if r.status == RunStatus.SUCCESS)
        failed = len(results) - succeeded
        lines = [f"Parallel spawn complete: {len(results)} agents finished ({succeeded} succeeded, {failed} failed)"]
        for r in results:
            lines.append(f"  {r.to_display()}")
        return ToolResult(success=True, output="\n".join(lines))


# ---------------------------------------------------------------------------
# MultiAgentConfig
# ---------------------------------------------------------------------------

@dataclass
class MultiAgentConfig:
    """
    Multi-Agent 配置。

    Attributes:
        max_agents:              Coordinator 最大可 spawn 的子 Agent 总数
        budget_ratio:            Coordinator / SubAgents 的 token 预算比例
        coordinator_max_steps:   Coordinator 最大步数
        worker_model:            子 Agent 默认模型（None = 使用父 backend 同款）
        worker_provider:         子 Agent 默认 provider
        max_parallel:            最大并行数
        timeout_per_agent:       每个 Agent 超时（秒）
        merge_approval_callback: Worktree 合并审批回调
        log_dir:                 日志目录
    """
    max_agents: int = 8
    budget_ratio: tuple[float, float] = (0.3, 0.7)
    coordinator_max_steps: int = 25
    worker_model: str | None = None
    worker_provider: str | None = None
    max_parallel: int = 3
    timeout_per_agent: float = 300.0
    merge_approval_callback: object = None
    log_dir: str | None = None


# ---------------------------------------------------------------------------
# CoordinatorAgent — 多 Agent 调度器
# ---------------------------------------------------------------------------

from agent.core import AgentConfig, ReActAgent


class CoordinatorAgent:
    """
    Multi-Agent 协调器。

    本身是 ReActAgent，通过注入 spawn_agent / list_agent_results 工具，
    让 LLM 自行决策何时 spawn 子 Agent、何时重试、何时结束。

    Agent 生命周期：
    1. LLM 调用 spawn_agent(role, task, ...)
    2. SubAgentExecutor 创建隔离的子 Agent 并执行
    3. 结果以摘要形式返回给 LLM
    4. LLM 决定下一步：继续 spawn / 重试 / reach_final_answer
    """

    def __init__(
        self,
        backend: "LLMBackend",
        registry: "ToolRegistry",
        config: "AgentConfig | None" = None,
        multi_config: MultiAgentConfig | None = None,
        memory_context: "MemoryContext | None" = None,
        worktree_mgr: "WorktreeManager | None" = None,
        repo_path: str = "",
        log_dir: str | None = None,
    ) -> None:
        self._backend = backend
        self._registry = registry
        self._config = config or AgentConfig()
        self._multi_cfg = multi_config or MultiAgentConfig()
        self._memory_context = memory_context
        self._worktree_mgr = worktree_mgr
        self._repo_path = repo_path
        self._log_dir = log_dir or self._multi_cfg.log_dir

        # 子 Agent 管理和状态追踪
        self._results: list[SubAgentResult] = []
        self._spawn_count = 0
        self._role_spawn_counts: dict[str, int] = {}
        self._final_finished = False
        self._final_summary = ""
        self._final_status = ""
        self._agent_worktrees: dict[str, Any] = {}  # agent_id → Worktree

        # SubAgentExecutor（延迟初始化，因为需要多 Agent 配置）
        self._sub_executor = SubAgentExecutor(
            backend=backend,
            full_registry=registry,
            parent_config=config,
            memory_context=memory_context,
            multi_config=self._multi_cfg,
        )

    def _wrap_coordinator_tools(self) -> "ToolRegistry":
        """构建 Coordinator 专用工具集（只有调度工具，不含读写工具）。"""
        from tools.base import ToolRegistry

        coord_registry = ToolRegistry()

        # 只注入 coordinator 调度工具 — 不给读写工具，强制通过子 agent 完成实际工作
        coord_registry._tools["spawn_agent"] = SpawnAgentTool(self)
        coord_registry._tools["spawn_parallel"] = SpawnParallelTool(self)
        coord_registry._tools["list_agent_results"] = ListAgentResultsTool(self)
        coord_registry._tools["finish_coordination"] = FinishCoordinationTool(self)

        return coord_registry

    def run(
        self,
        task: "Task",
        event_log: "EventLog",
        session_dir: str | None = None,
    ) -> RunResult:
        """启动 Multi-Agent 协调器。"""
        from agent.prompt import build_coordinator_prompt

        # 重置每轮状态（同一个 CoordinatorAgent 实例可能跨轮复用）
        self._results = []
        self._spawn_count = 0
        self._role_spawn_counts = {}
        self._final_finished = False
        self._final_summary = ""
        self._final_status = ""

        # 构建 Coordinator 的 system prompt
        coordinator_prompt = build_coordinator_prompt(
            task_description=task.description,
            max_agents=self._multi_cfg.max_agents,
        )

        # 生成 start 消息
        start_msg = LLMMessage(role="user", content=coordinator_prompt)
        logger.info("Starting CoordinatorAgent for task %s", task.task_id)

        # 用增强后的注册表创建 Coordinator ReActAgent
        enhanced_tools = self._wrap_coordinator_tools()

        # Coordinator 用更大的 budget：它的上下文主要是子 agent 的结果摘要
        from agent.core import AgentConfig
        coord_cfg = AgentConfig(
            max_steps=self._multi_cfg.coordinator_max_steps,
            budget_tokens=max((self._config.budget_tokens if self._config else 80_000), 120_000),
        )

        coordinator_agent = ReActAgent(
            backend=self._backend,
            registry=enhanced_tools,
            config=coord_cfg,
            memory_context=None,  # coordinator 不需要记忆检索
        )

        # 注入 Coordinator prompt + 对话上下文
        coordinator_agent._pending_history = ConversationHistory(max_messages=50)

        # 如果有跨轮对话历史（来自 ChatSession），注入摘要作为前置上下文
        if hasattr(self, "_pending_history") and self._pending_history:
            prior_msgs = self._pending_history.to_dicts()
            # 取之前轮次的 assistant 回复摘要（跳过当前轮的 user 消息）
            prior_summaries = [
                m["content"] for m in prior_msgs
                if m.get("role") == "assistant" and m.get("content")
            ]
            if prior_summaries:
                context_text = "\n---\n".join(prior_summaries[-3:])  # 最近 3 轮
                coordinator_agent._pending_history.add(LLMMessage(
                    role="user",
                    content=f"[Previous conversation context]\n{context_text}",
                ))
                coordinator_agent._pending_history.add(LLMMessage(
                    role="assistant",
                    content="Understood. I have the previous conversation context.",
                ))

        coordinator_agent._pending_history.add(start_msg)

        # 执行
        result = coordinator_agent.run(task, event_log)

        # 如果 finish_coordination 被调用过，使用它的 summary
        if self._final_finished and self._final_summary:
            result.summary = self._final_summary

        # 将子 Agent 的总 token 计入
        total_sub_tokens = sum(r.total_tokens for r in self._results)
        result.total_tokens = (result.total_tokens or 0) + total_sub_tokens

        logger.info(
            "CoordinatorAgent finished: status=%s, steps=%d, coord_tokens=%d, sub_tokens=%d",
            result.status.value, result.steps_taken,
            result.total_tokens - total_sub_tokens if result.total_tokens else 0,
            total_sub_tokens,
        )

        return result

    def _has_budget_for_spawn(self) -> bool:
        """检查是否还有预算 spawn 新的子 Agent。"""
        if self._spawn_count >= self._multi_cfg.max_agents:
            return False
        # 检查 token 预算
        sub_budget = getattr(self, "_sub_budget", None)
        if sub_budget is not None:
            tokens_used = getattr(self, "_tokens_used_by_subs", 0)
            if tokens_used >= sub_budget:
                return False
        return True

    def _steps_for_role(self, role: SubAgentRole) -> int:
        """根据角色返回建议的 max_steps。"""
        defaults = {
            SubAgentRole.EXPLORER: 8,
            SubAgentRole.PLANNER: 8,
            SubAgentRole.CODER: 25,
            SubAgentRole.REVIEWER: 10,
            SubAgentRole.TESTER: 10,
        }
        return defaults.get(role, 10)

    def _budget_for_role(self, role: SubAgentRole) -> int:
        """根据角色分配 token 预算（加权，受剩余预算约束）。"""
        # 使用 _sub_budget（如果由测试设置）或从配置计算
        sub_budget = getattr(self, "_sub_budget", None)
        if sub_budget is None:
            total = self._config.budget_tokens or 80_000
            sub_budget = int(total * self._multi_cfg.budget_ratio[1])
        tokens_used = getattr(self, "_tokens_used_by_subs", 0)
        remaining = sub_budget - tokens_used

        weights = {
            SubAgentRole.EXPLORER: 0.30,
            SubAgentRole.PLANNER: 0.20,
            SubAgentRole.CODER: 0.35,
            SubAgentRole.REVIEWER: 0.25,
            SubAgentRole.TESTER: 0.20,
        }
        weight = weights.get(role, 0.25)
        allocated = int(sub_budget * weight)
        allocated = max(allocated, 15_000)
        return min(allocated, remaining, 80_000)

    def _get_result(self, agent_id: str) -> SubAgentResult | None:
        """按 agent_id 查找子 Agent 结果。"""
        for r in self._results:
            if r.agent_id == agent_id:
                return r
        return None

    def _init_worktree_manager(self, repo_path: str):
        """初始化 WorktreeManager（仅在合法 git 仓库中）。"""
        import subprocess
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=repo_path,
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return None
        except Exception:
            return None

        from tools.snapshot import WorktreeManager
        return WorktreeManager(repo_path)

    def _finalize_worktrees(self) -> list[str]:
        """
        协调结束后处理所有 worktree：成功的合并，失败的丢弃。

        Returns:
            错误列表（合并冲突等）
        """
        errors: list[str] = []
        if not self._worktree_mgr:
            return errors

        callback = getattr(self._multi_cfg, "merge_approval_callback", None)

        for agent_id, wt in list(self._agent_worktrees.items()):
            result = self._get_result(agent_id)
            if result and result.status == RunStatus.SUCCESS:
                try:
                    approved = True
                    if callback:
                        diff = self._worktree_mgr.get_diff(wt)
                        approved = callback(wt.name, diff)

                    if approved:
                        self._worktree_mgr.merge(wt, delete_after=True)
                    else:
                        self._worktree_mgr.discard(wt)
                        errors.append(f"{wt.name}: merge rejected by callback")
                except Exception as e:
                    errors.append(f"{wt.name}: {e}")
                    try:
                        self._worktree_mgr.discard(wt)
                    except Exception:
                        pass
            else:
                try:
                    self._worktree_mgr.discard(wt)
                except Exception:
                    pass

        self._agent_worktrees.clear()
        return errors
