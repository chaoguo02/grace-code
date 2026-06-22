"""
agent/dag.py

DAG-based Plan Executor.

将复杂任务分解为带依赖关系的 subtask DAG，按拓扑层级逐层执行。
每个 subtask 在独立的 ReActAgent 中运行，上游结果自动注入下游。
"""

from __future__ import annotations

import logging
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

from agent.event_log import EventLog
from agent.plan import Plan, PlanGenerationError, SubTask, SubTaskStatus, SubTaskType
from agent.task import RunResult, RunStatus, Task
from context.history import ConversationHistory
from llm.base import LLMMessage

if TYPE_CHECKING:
    from agent.core import AgentConfig, ReActAgent
    from agent.plan import PlanExecuteConfig
    from llm.base import LLMBackend
    from memory.context import MemoryContext
    from tools.base import ToolRegistry

logger = logging.getLogger(__name__)


_READONLY_TOOLS = frozenset({
    "file_read", "file_view", "find_files", "find_symbol", "search_text",
    "git_status", "git_diff", "web_search", "web_fetch",
})

SUBTASK_TOOL_WHITELIST: dict[SubTaskType, frozenset[str]] = {
    SubTaskType.PLANNING: _READONLY_TOOLS,
    SubTaskType.FILE_READ: frozenset({
        "file_read", "file_view", "find_files", "find_symbol", "search_text", "git_status", "git_diff",
    }),
    SubTaskType.ANALYSIS: _READONLY_TOOLS,
    SubTaskType.FILE_WRITE: _READONLY_TOOLS | frozenset({"file_write", "file_edit", "edit"}),
    SubTaskType.COMMAND: _READONLY_TOOLS | frozenset({"shell", "test", "pytest"}),
    SubTaskType.VERIFICATION: _READONLY_TOOLS | frozenset({"shell", "test", "pytest"}),
}

PARALLEL_SAFE_TYPES = frozenset({
    SubTaskType.PLANNING,
    SubTaskType.FILE_READ,
    SubTaskType.ANALYSIS,
})


# ---------------------------------------------------------------------------
# DAG 验证
# ---------------------------------------------------------------------------

class DAGValidationError(Exception):
    """DAG 结构无效（环、缺失引用等）。"""
    pass


def validate_dag(subtasks: list[SubTask]) -> None:
    """
    验证 subtask 列表构成合法 DAG。

    检查:
    1. 无重复 id
    2. 所有 depends_on 引用的 id 存在
    3. 无环（Kahn's 算法）

    Raises:
        DAGValidationError
    """
    ids = {st.id for st in subtasks}

    # 检查重复 id
    if len(ids) != len(subtasks):
        seen: set[str] = set()
        dupes: set[str] = set()
        for st in subtasks:
            if st.id in seen:
                dupes.add(st.id)
            seen.add(st.id)
        raise DAGValidationError(
            f"Duplicate subtask id(s) found: {sorted(dupes)}"
        )

    # 检查引用完整性
    for st in subtasks:
        for dep in st.depends_on:
            if dep not in ids:
                raise DAGValidationError(
                    f"Subtask '{st.id}' depends on '{dep}' which does not exist"
                )

    # Kahn's 算法检测环
    in_degree: dict[str, int] = {st.id: 0 for st in subtasks}
    children: dict[str, list[str]] = {st.id: [] for st in subtasks}

    for st in subtasks:
        for dep in st.depends_on:
            children[dep].append(st.id)
            in_degree[st.id] += 1

    queue = deque(nid for nid, deg in in_degree.items() if deg == 0)
    visited = 0

    while queue:
        node = queue.popleft()
        visited += 1
        for child in children[node]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if visited != len(subtasks):
        raise DAGValidationError(
            f"DAG contains a cycle (processed {visited}/{len(subtasks)} nodes)"
        )


# ---------------------------------------------------------------------------
# 拓扑分层
# ---------------------------------------------------------------------------

def normalize_dag(subtasks: list[SubTask]) -> None:
    """根据 depends_on 自动补齐 dependents，避免要求 LLM 维护双向依赖。"""
    id_to_task = {st.id: st for st in subtasks}
    for st in subtasks:
        st.dependents = []
    for st in subtasks:
        for dep in st.depends_on:
            if dep in id_to_task and st.id not in id_to_task[dep].dependents:
                id_to_task[dep].dependents.append(st.id)


def compute_critical_path(subtasks: list[SubTask]) -> tuple[list[str], float]:
    """基于 subtask.duration_ms 计算 DAG 最长耗时路径。"""
    if not subtasks:
        return [], 0.0

    id_to_task = {st.id: st for st in subtasks}
    layers = topological_layers(subtasks)
    best_duration: dict[str, float] = {}
    previous: dict[str, str | None] = {}

    for layer in layers:
        for st in layer:
            upstream = [dep for dep in st.depends_on if dep in id_to_task]
            if upstream:
                best_dep = max(upstream, key=lambda dep: best_duration.get(dep, 0.0))
                best_duration[st.id] = best_duration.get(best_dep, 0.0) + st.duration_ms
                previous[st.id] = best_dep
            else:
                best_duration[st.id] = st.duration_ms
                previous[st.id] = None

    end_id = max(best_duration, key=best_duration.get)
    path = []
    cursor: str | None = end_id
    while cursor is not None:
        path.append(cursor)
        cursor = previous.get(cursor)
    path.reverse()
    return path, best_duration[end_id]


def _extract_declared_paths(subtask: SubTask) -> set[str]:
    """从子任务描述中保守提取文件路径，用于冲突检测辅助。"""
    pattern = r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+|[A-Za-z0-9_.-]+\.[A-Za-z0-9_]+"
    return {match.replace("\\", "/") for match in re.findall(pattern, subtask.description)}


def _has_path_conflicts(layer: list[SubTask]) -> bool:
    seen: set[str] = set()
    for subtask in layer:
        if subtask.type != SubTaskType.FILE_WRITE:
            continue
        paths = _extract_declared_paths(subtask)
        if seen.intersection(paths):
            return True
        seen.update(paths)
    return False


def build_replan_context(plan: Plan, failure_reason: str) -> str:
    """构建重新规划上下文：已完成、失败、跳过和失败原因。"""
    completed = [st for st in plan.subtasks if st.status == SubTaskStatus.COMPLETED]
    failed = [st for st in plan.subtasks if st.status == SubTaskStatus.FAILED]
    skipped = [st for st in plan.subtasks if st.status == SubTaskStatus.SKIPPED]

    parts = [f"Failure reason:\n{failure_reason}"]
    if completed:
        parts.append("Completed subtasks:")
        parts.extend(f"- {st.id} ({st.type.value}): {st.result_summary[:200]}" for st in completed)
    if failed:
        parts.append("Failed subtasks:")
        parts.extend(f"- {st.id} ({st.type.value}): {(st.error or st.result_summary)[:200]}" for st in failed)
    if skipped:
        parts.append("Skipped downstream subtasks:")
        parts.extend(f"- {st.id} ({st.type.value}): {st.skip_reason[:200]}" for st in skipped)
    parts.append("Replan constraints:\n- Do not repeat completed subtasks.\n- Plan only the remaining work.\n- Keep dependencies valid and minimal.")
    return "\n".join(parts)


def render_dag_mermaid(subtasks: list[SubTask], critical_path: list[str] | None = None) -> str:
    """生成 Mermaid DAG 文本，用于日志/摘要中的依赖图展示。"""
    critical_edges = set()
    if critical_path:
        critical_edges = set(zip(critical_path, critical_path[1:]))

    lines = ["graph TD"]
    for st in subtasks:
        label = f"{st.id} {st.type.value}\\n{st.status.value} {st.duration_ms:.0f}ms"
        lines.append(f"  {st.id}[\"{label}\"]")
    edge_index = 0
    critical_edge_indexes = []
    for st in subtasks:
        for dep in st.depends_on:
            edge = f"  {dep} --> {st.id}"
            lines.append(edge)
            if (dep, st.id) in critical_edges:
                critical_edge_indexes.append(edge_index)
            edge_index += 1
    for idx in critical_edge_indexes:
        lines.append(f"  linkStyle {idx} stroke:#f66,stroke-width:3px")
    return "\n".join(lines)


def topological_layers(subtasks: list[SubTask]) -> list[list[SubTask]]:
    """
    将 subtask 列表按拓扑排序分层。

    Layer 0: 无依赖的 subtask
    Layer N: 所有依赖在 Layer < N 中的 subtask

    前提：subtasks 已通过 validate_dag() 验证。
    """
    id_to_task = {st.id: st for st in subtasks}
    in_degree: dict[str, int] = {st.id: len(st.depends_on) for st in subtasks}
    children: dict[str, list[str]] = {st.id: [] for st in subtasks}

    for st in subtasks:
        for dep in st.depends_on:
            children[dep].append(st.id)

    layers: list[list[SubTask]] = []
    current = [nid for nid, deg in in_degree.items() if deg == 0]

    while current:
        layer = [id_to_task[nid] for nid in current]
        layers.append(layer)
        next_level = []
        for nid in current:
            for child in children[nid]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    next_level.append(child)
        current = next_level

    return layers


# ---------------------------------------------------------------------------
# DAGPlanner
# ---------------------------------------------------------------------------

class DAGPlanner:
    """负责生成和重新生成 DAG Plan，执行器只消费已解析的 Plan。"""

    def __init__(
        self,
        backend: "LLMBackend",
        registry: "ToolRegistry",
        agent_config: "AgentConfig",
        plan_config: "PlanExecuteConfig",
        memory_context: "MemoryContext | None" = None,
    ) -> None:
        self._backend = backend
        self._registry = registry
        self._cfg = agent_config
        self._plan_cfg = plan_config
        self._memory_context = memory_context

    def create_plan(self, task: Task) -> tuple[Plan | None, int, int]:
        """用只读 ReActAgent 探索并生成结构化 DAG Plan。"""
        from agent.prompt import get_dag_plan_prompt

        prompt = (
            f"{get_dag_plan_prompt()}\n\n"
            f"## Repository\n{task.repo_path}\n\n"
            f"## Task\n{task.description}\n\n"
            f"Explore the codebase and produce a DAG execution plan. "
            f"Stop calling tools and respond with the JSON plan when ready."
        )
        return self._run_planning_agent(task, prompt)

    def replan(
        self,
        original_task: Task,
        failed_plan: Plan,
        failure_reason: str,
    ) -> tuple[Plan | None, int, int]:
        """基于已完成进度和失败原因，为剩余工作重新规划。"""
        from agent.prompt import get_dag_plan_prompt

        context = build_replan_context(failed_plan, failure_reason)
        prompt = (
            f"{get_dag_plan_prompt()}\n\n"
            "[REPLAN MODE] Replan ONLY the remaining work. Do not repeat completed subtasks.\n\n"
            f"## Original Task\n{original_task.description}\n\n"
            f"## Current Execution Context\n{context}\n\n"
            "Produce a new JSON DAG plan for the remaining work only."
        )
        return self._run_planning_agent(original_task, prompt)

    def _run_planning_agent(self, task: Task, prompt: str) -> tuple[Plan | None, int, int]:
        from agent.core import ReActAgent

        agent = ReActAgent(
            self._backend, self._registry, self._cfg,
            memory_context=self._memory_context,
        )
        history = ConversationHistory(max_messages=self._cfg.history_max_messages)
        history.add(LLMMessage(role="user", content=prompt))
        agent._pending_history = history

        plan_steps = min(8, max(5, task.max_steps // 3))
        plan_task = Task(
            description=task.description,
            repo_path=task.repo_path,
            max_steps=plan_steps,
            budget_tokens=task.budget_tokens // 3,
        )

        agent.switch_to_plan_mode()
        plan_log = EventLog.create(plan_task, log_dir=self._plan_cfg.plan_subtask_log_dir)
        plan_result = agent.run(plan_task, plan_log)
        plan_log.close()

        plan_text = plan_result.summary or ""
        if not plan_text.strip():
            logger.warning("DAG plan generation produced empty result")
            return None, 0, 0

        try:
            plan = Plan.from_dag_json(plan_text, task.description)
            validate_dag(plan.subtasks)
            normalize_dag(plan.subtasks)
        except (PlanGenerationError, DAGValidationError) as e:
            logger.warning("DAG plan validation failed: %s — trying fallback JSON parse", e)
            try:
                plan = Plan.from_json(plan_text, task.description)
                validate_dag(plan.subtasks)
                normalize_dag(plan.subtasks)
            except (PlanGenerationError, DAGValidationError):
                logger.warning("Fallback JSON parse also failed — falling back to react")
                return None, plan_result.total_tokens, plan_result.steps_taken

        return plan, plan_result.total_tokens, plan_result.steps_taken


# ---------------------------------------------------------------------------
# DAGPlanExecutor
# ---------------------------------------------------------------------------

class DAGPlanExecutor:
    """
    DAG 版 Plan-then-Execute Agent.

    三阶段：
    1. _generate_plan(): 只读 ReActAgent 探索 → LLM 输出 DAG JSON
    2. 用户审批
    3. _execute_dag(): 按拓扑层级逐层执行
    """

    def __init__(
        self,
        backend: "LLMBackend",
        registry: "ToolRegistry",
        agent_config: "AgentConfig | None" = None,
        plan_config: "PlanExecuteConfig | None" = None,
        memory_context: "MemoryContext | None" = None,
    ) -> None:
        from agent.core import AgentConfig
        from agent.plan import PlanExecuteConfig

        self._backend = backend
        self._registry = registry
        self._cfg = agent_config or AgentConfig()
        self._plan_cfg = plan_config or PlanExecuteConfig()
        self._memory_context = memory_context

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def run(self, task: Task, log: EventLog) -> RunResult:
        """三阶段 DAG 执行。"""
        log.log_task_start(task)
        logger.info("DAGPlanExecutor starting task %s", task.task_id)

        # Phase 1: 生成 DAG Plan
        planner = DAGPlanner(
            self._backend, self._registry, self._cfg, self._plan_cfg,
            memory_context=self._memory_context,
        )
        plan, plan_tokens, plan_steps = planner.create_plan(task)
        if plan is None:
            return self._fallback_react(task, log)

        log.log_plan_generated(plan)
        logger.info(
            "DAG plan generated: %d subtasks, %d layers",
            len(plan.subtasks),
            len(topological_layers(plan.subtasks)),
        )

        # Phase 2: 用户审批
        approval_cb = self._plan_cfg.plan_approval_callback
        if approval_cb:
            display = self._format_dag_for_display(plan)
            approved = approval_cb(display)
            if not approved:
                log.log_task_failed(steps=plan_steps, reason="Plan rejected by user")
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.GAVE_UP,
                    summary="Plan rejected by user",
                    steps_taken=plan_steps,
                    total_tokens=plan_tokens,
                )

        # Phase 3: 执行 DAG
        exec_result = self._execute_dag(plan, task, log, plan_tokens, plan_steps)
        if (
            exec_result.status == RunStatus.FAILED
            and self._plan_cfg.enable_replan
            and self._plan_cfg.max_replans > 0
        ):
            replan_reason = exec_result.summary or exec_result.error or "DAG execution failed"
            replan, replan_tokens, replan_steps = planner.replan(task, plan, replan_reason)
            if replan is not None:
                logger.info("Executing replan with %d subtasks", len(replan.subtasks))
                log.log_replan_generated(replan, attempt=1, reason=replan_reason)
                replan_result = self._execute_dag(
                    replan,
                    task,
                    log,
                    plan_tokens + exec_result.total_tokens + replan_tokens,
                    plan_steps + exec_result.steps_taken + replan_steps,
                )
                replan_result.summary = (
                    "Original DAG failed; replan #1 executed.\n"
                    f"Original failure:\n{replan_reason}\n\n"
                    f"Replan result:\n{replan_result.summary}"
                )
                return replan_result
        return exec_result

    # ------------------------------------------------------------------
    # Phase 1: 生成 DAG Plan
    # ------------------------------------------------------------------

    def _generate_plan(
        self, task: Task, log: EventLog
    ) -> tuple[Plan | None, int, int]:
        """
        用只读 ReActAgent 探索 + 请求结构化 DAG JSON.

        Returns:
            (plan, tokens_used, steps_taken) 或 (None, 0, 0)
        """
        planner = DAGPlanner(
            self._backend, self._registry, self._cfg, self._plan_cfg,
            memory_context=self._memory_context,
        )
        return planner.create_plan(task)

    # ------------------------------------------------------------------
    # Phase 3: DAG 执行
    # ------------------------------------------------------------------

    def _execute_dag(
        self, plan: Plan, task: Task, log: EventLog,
        plan_tokens: int, plan_steps: int,
    ) -> RunResult:
        """按拓扑层级顺序执行 subtask."""
        from agent.core import ReActAgent

        layers = topological_layers(plan.subtasks)
        total_tokens = plan_tokens
        total_steps = plan_steps

        budget_per_subtask = max(
            10000,
            (task.budget_tokens - plan_tokens) // max(len(plan.subtasks), 1),
        )
        steps_per_subtask = min(
            10,
            max(5, (task.max_steps - plan_steps) // max(len(plan.subtasks), 1)),
        )

        id_to_task = {st.id: st for st in plan.subtasks}
        failed_ids: set[str] = set()

        for layer_idx, layer in enumerate(layers):
            logger.info("Executing DAG layer %d (%d subtasks)", layer_idx, len(layer))

            runnable = self._prepare_layer(layer, failed_ids, log)
            if self._can_run_layer_parallel(runnable):
                results = self._execute_layer_parallel(
                    runnable, task, id_to_task,
                    budget_tokens=budget_per_subtask,
                    max_steps=steps_per_subtask,
                    log=log,
                    total_subtasks=len(plan.subtasks),
                )
                for subtask, result in results:
                    total_tokens += result.total_tokens
                    total_steps += result.steps_taken
                    self._finalize_subtask(subtask, result, failed_ids, log)
            else:
                for subtask in runnable:
                    subtask.mark_started()
                    log.log_subtask_start(subtask, index=total_steps + 1, total=len(plan.subtasks))
                    result = self._execute_single_subtask(
                        subtask, task, id_to_task,
                        budget_tokens=budget_per_subtask,
                        max_steps=steps_per_subtask,
                    )
                    total_tokens += result.total_tokens
                    total_steps += result.steps_taken
                    self._finalize_subtask(subtask, result, failed_ids, log)

        # 汇总结果
        completed_tasks = [st for st in plan.subtasks if st.status == SubTaskStatus.COMPLETED]
        failed_tasks = [st for st in plan.subtasks if st.status == SubTaskStatus.FAILED]
        skipped_tasks = [st for st in plan.subtasks if st.status == SubTaskStatus.SKIPPED]

        summary = self._build_execution_summary(plan, completed_tasks, failed_tasks, skipped_tasks, log)

        if failed_tasks:
            from agent.core import _git_diff
            patch = _git_diff(task.repo_path)
            log.log_task_failed(steps=total_steps, reason=summary)
            return RunResult(
                task_id=task.task_id,
                status=RunStatus.FAILED,
                summary=summary,
                steps_taken=total_steps,
                total_tokens=total_tokens,
                patch=patch,
            )

        from agent.core import _git_diff
        patch = _git_diff(task.repo_path)
        log.log_task_complete(steps=total_steps, summary=summary)
        return RunResult(
            task_id=task.task_id,
            status=RunStatus.SUCCESS,
            summary=summary,
            steps_taken=total_steps,
            total_tokens=total_tokens,
            patch=patch,
        )

    def _execute_single_subtask(
        self,
        subtask: SubTask,
        parent_task: Task,
        id_to_task: dict[str, SubTask],
        budget_tokens: int,
        max_steps: int,
        thread_isolated: bool = False,
    ) -> RunResult:
        """在独立 ReActAgent 中执行单个 subtask."""
        from agent.core import AgentConfig, ReActAgent
        from agent.prompt import build_dag_subtask_prompt

        upstream_context = self._build_upstream_context(subtask, id_to_task)

        cfg = AgentConfig(
            max_steps=max_steps,
            budget_tokens=budget_tokens,
            history_max_messages=self._cfg.history_max_messages,
            llm_max_retries=self._cfg.llm_max_retries,
            llm_retry_delay=self._cfg.llm_retry_delay,
            stream=False if thread_isolated else self._cfg.stream,
            stream_callback=None if thread_isolated else self._cfg.stream_callback,
            thought_callback=None if thread_isolated else self._cfg.thought_callback,
            confirm_dangerous=self._cfg.confirm_dangerous,
            confirm_callback=self._cfg.confirm_callback,
        )

        agent = ReActAgent(
            self._backend, self._registry_for_subtask(subtask), cfg,
            memory_context=None if thread_isolated else self._memory_context,
        )

        prompt = build_dag_subtask_prompt(
            subtask_id=subtask.id,
            description=subtask.description,
            expected_outcome=subtask.expected_outcome,
            upstream_context=upstream_context,
        )

        sub_task = Task(
            description=prompt,
            repo_path=parent_task.repo_path,
            max_steps=max_steps,
            budget_tokens=budget_tokens,
        )

        sub_log = EventLog.create(sub_task, log_dir=self._plan_cfg.plan_subtask_log_dir)
        result = agent.run(sub_task, sub_log)
        sub_log.close()

        return result

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _prepare_layer(self, layer: list[SubTask], failed_ids: set[str], log: EventLog) -> list[SubTask]:
        """处理依赖失败的 skipped 节点，返回本层仍需执行的节点。"""
        runnable = []
        for subtask in layer:
            failed_deps = [dep for dep in subtask.depends_on if dep in failed_ids]
            if failed_deps:
                reason = f"Skipped because dependency failed: {', '.join(failed_deps)}"
                subtask.mark_skipped(reason)
                failed_ids.add(subtask.id)
                log.log_subtask_skipped(subtask, reason)
                logger.info("Skipping subtask %s (%s)", subtask.id, reason)
            else:
                runnable.append(subtask)
        return runnable

    def _can_run_layer_parallel(self, layer: list[SubTask]) -> bool:
        """仅并行明确安全的层；写入/命令并行默认关闭并受冲突检测保护。"""
        if len(layer) <= 1 or _has_path_conflicts(layer):
            return False
        layer_types = {st.type for st in layer}
        if layer_types.issubset(PARALLEL_SAFE_TYPES):
            return True
        if layer_types == {SubTaskType.VERIFICATION}:
            return self._plan_cfg.allow_parallel_verification
        if layer_types == {SubTaskType.COMMAND}:
            return self._plan_cfg.allow_parallel_commands
        return False

    def _execute_layer_parallel(
        self,
        layer: list[SubTask],
        parent_task: Task,
        id_to_task: dict[str, SubTask],
        budget_tokens: int,
        max_steps: int,
        log: EventLog,
        total_subtasks: int,
    ) -> list[tuple[SubTask, RunResult]]:
        """并行执行同一拓扑层中的安全子任务。"""
        results: list[tuple[SubTask, RunResult]] = []
        max_workers = min(len(layer), 4)
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="dag-layer") as pool:
            future_to_subtask = {}
            for subtask in layer:
                subtask.mark_started()
                log.log_subtask_start(subtask, index=0, total=total_subtasks)
                future = pool.submit(
                    self._execute_single_subtask,
                    subtask,
                    parent_task,
                    id_to_task,
                    budget_tokens,
                    max_steps,
                    True,
                )
                future_to_subtask[future] = subtask

            for future in as_completed(future_to_subtask):
                subtask = future_to_subtask[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = RunResult(
                        task_id=subtask.id,
                        status=RunStatus.FAILED,
                        summary=f"Parallel subtask raised: {exc}",
                        steps_taken=0,
                        error=str(exc),
                    )
                results.append((subtask, result))
        return results

    def _finalize_subtask(
        self,
        subtask: SubTask,
        result: RunResult,
        failed_ids: set[str],
        log: EventLog,
    ) -> None:
        if result.is_success():
            subtask.mark_completed(result.summary or "")
            log.log_subtask_complete(subtask, result)
        else:
            error = result.summary or result.error or "Failed"
            subtask.mark_failed(error)
            log.log_subtask_failed(subtask, result)
            failed_ids.add(subtask.id)
            logger.warning("Subtask %s failed: %s", subtask.id, subtask.result_summary[:100])

    def _registry_for_subtask(self, subtask: SubTask) -> "ToolRegistry":
        """按 SubTaskType 过滤工具，给 DAG 节点提供硬权限边界。"""
        from tools.base import ToolRegistry

        allowed = SUBTASK_TOOL_WHITELIST.get(subtask.type, _READONLY_TOOLS)
        return self._registry.filtered(allowed)

    def _build_execution_summary(
        self,
        plan: Plan,
        completed_tasks: list[SubTask],
        failed_tasks: list[SubTask],
        skipped_tasks: list[SubTask],
        log: EventLog,
    ) -> str:
        """生成紧凑但可诊断的 DAG 执行摘要。"""
        summary_parts = [
            f"DAG execution complete: {len(completed_tasks)} completed, "
            f"{len(failed_tasks)} failed, {len(skipped_tasks)} skipped."
        ]

        for st in plan.subtasks:
            detail = st.result_summary or st.error or st.skip_reason
            duration = f"{st.duration_ms:.0f}ms" if st.duration_ms else "0ms"
            line = f"  [{st.id}] {st.type.value} {st.status.value} in {duration}"
            if detail:
                line += f": {detail[:120]}"
            summary_parts.append(line)

        slowest = sorted(
            [st for st in plan.subtasks if st.duration_ms],
            key=lambda st: st.duration_ms,
            reverse=True,
        )[:3]
        if slowest:
            summary_parts.append("Slowest subtasks:")
            for st in slowest:
                summary_parts.append(f"  [{st.id}] {st.type.value}: {st.duration_ms:.0f}ms")

        type_stats = self._build_type_stats(plan)
        if type_stats:
            summary_parts.append("By type:")
            summary_parts.extend(type_stats)

        critical_path, critical_duration = compute_critical_path(plan.subtasks)
        if critical_path:
            summary_parts.append(
                f"Critical path: {' -> '.join(critical_path)} ({critical_duration:.0f}ms)"
            )
            log.log_dag_graph(
                render_dag_mermaid(plan.subtasks, critical_path),
                critical_path,
                critical_duration,
            )

        for st in failed_tasks:
            affected = [dep for dep in st.dependents if dep]
            if affected:
                summary_parts.append(f"Subtask {st.id} affected downstream: {', '.join(affected)}")

        return "\n".join(summary_parts)

    def _build_type_stats(self, plan: Plan) -> list[str]:
        totals: dict[str, dict[str, float | int]] = {}
        for st in plan.subtasks:
            stats = totals.setdefault(st.type.value, {"count": 0, "failed": 0, "duration": 0.0})
            stats["count"] = int(stats["count"]) + 1
            stats["failed"] = int(stats["failed"]) + (1 if st.status == SubTaskStatus.FAILED else 0)
            stats["duration"] = float(stats["duration"]) + st.duration_ms
        return [
            f"  {typ}: {int(stats['count'])} tasks, {float(stats['duration']):.0f}ms, {int(stats['failed'])} failed"
            for typ, stats in sorted(totals.items())
        ]

    def _build_upstream_context(
        self, subtask: SubTask, id_to_task: dict[str, SubTask]
    ) -> str:
        """从已完成的上游 subtask 中收集 result_summary."""
        parts = []
        for dep_id in subtask.depends_on:
            dep = id_to_task.get(dep_id)
            if dep and dep.status == SubTaskStatus.COMPLETED and dep.result_summary:
                parts.append(f"[Subtask {dep.id}] {dep.result_summary}")
        return "\n".join(parts)

    def _format_dag_for_display(self, plan: Plan) -> str:
        """将 DAG plan 格式化为人类可读的审批文本。"""
        layers = topological_layers(plan.subtasks)
        lines = []
        if plan.reasoning:
            lines.append(f"## Reasoning\n{plan.reasoning}\n")
        lines.append("## Execution Plan (DAG)\n")
        for i, layer in enumerate(layers):
            lines.append(f"### Layer {i} (parallel-ready)")
            for st in layer:
                deps = f" [depends: {', '.join(st.depends_on)}]" if st.depends_on else ""
                lines.append(f"  {st.id}. {st.description}{deps}")
                if st.expected_outcome:
                    lines.append(f"     Expected: {st.expected_outcome}")
            lines.append("")
        return "\n".join(lines)

    def _fallback_react(self, task: Task, log: EventLog) -> RunResult:
        """Plan 生成失败时回退到普通 ReActAgent."""
        from agent.core import ReActAgent

        logger.info("Falling back to plain ReActAgent")
        agent = ReActAgent(
            self._backend, self._registry, self._cfg,
            memory_context=self._memory_context,
        )
        return agent.run(task, log)
