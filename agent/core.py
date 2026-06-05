"""
agent/core.py

ReAct 主循环。整个 agent 的大脑。

职责（只做这些，不做别的）：
- 维护对话历史，每轮组装 messages 调用 LLM
- 拿到 Action 后调用 ToolRegistry 执行
- 把 Action + Observation 写入 EventLog
- 检测三种终止/Reflection 触发条件
- 返回 RunResult

不负责：
- 任何 LLM 细节（交给 LLMBackend）
- 任何工具实现（交给 Tool）
- 上下文压缩（由 context/ 模块负责）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from agent.event_log import EventLog
from context.history import ConversationHistory
from context.repo_map import RepoMap
from context.token_budget import TokenBudget
from agent.prompt import (
    build_system_prompt,
    build_task_prompt,
    reflection_no_edit,
    reflection_test_failed,
)
from agent.task import (
    Action, ActionType, Event, EventType,
    Observation, ObservationStatus, RunResult, RunStatus, Task, ToolCall,
)
from llm.base import LLMBackend, LLMMessage, LLMToolSchema
from tools.base import ToolRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    """Agent 运行时配置，从 config/default.yaml 加载后传入。"""
    max_steps: int = 40
    reflection_no_edit_steps: int = 6   # 连续 N 步无文件写操作触发 Reflection
    loop_detection_window: int = 3       # 连续 N 步完全相同 action 判定死循环
    test_tool_names: tuple[str, ...] = ("test", "pytest")  # 触发 Reflection 的工具名
    budget_tokens: int = 80_000            # 总 token 预算
    history_max_messages: int = 40         # 历史最大条数
    llm_max_retries: int = 3               # LLM 调用失败最大重试次数
    llm_retry_delay: float = 2.0           # 重试间隔（秒，指数退避）
    stream: bool = False                   # 是否启用流式输出
    stream_callback: object = None         # StreamCallback，最终回答流式回调
    thought_callback: object = None        # StreamCallback，推理过程流式回调（推理模型专用）
    confirm_dangerous: bool = False        # 是否对危险命令要求用户确认
    confirm_callback: object = None        # ConfirmCallback，None=跳过确认



# ---------------------------------------------------------------------------
# 共享工具函数
# ---------------------------------------------------------------------------

def _git_diff(repo_path: str) -> str | None:
    """抓取 git diff HEAD 作为 patch，失败时静默返回 None。"""
    import subprocess
    try:
        proc = subprocess.run(
            ["git", "diff", "HEAD"],
            capture_output=True, text=True, timeout=10, cwd=repo_path,
        )
        diff = proc.stdout.strip()
        return diff if diff else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# ReActAgent — ReAct (Reasoning + Acting) 主循环
# ---------------------------------------------------------------------------

class ReActAgent:
    """
    ReAct 主循环实现。

    用法：
        agent = ReActAgent(backend, registry, config)
        result = agent.run(task, log)

    这是一个纯粹的 ReAct agent：每步 思考→行动→观察，循环直到完成或超限。
    对于需要"先规划后执行"的多步骤复杂任务，使用 PlanExecuteAgent 作为编排层。
    """

    def __init__(
        self,
        backend: LLMBackend,
        registry: ToolRegistry,
        config: AgentConfig | None = None,
    ) -> None:
        self._backend = backend
        self._registry = registry
        self._cfg = config or AgentConfig()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def run(self, task: Task, log: EventLog) -> RunResult:
        """
        执行一次完整的 agent 运行。

        Args:
            task: 任务描述
            log:  已初始化的 EventLog（由调用方创建并传入）

        Returns:
            RunResult，包含最终状态和统计信息
        """
        self._current_repo_path = task.repo_path
        # 按 repo_path 隔离 repo_map 缓存，换 repo 时自动重建
        cache_key = task.repo_path
        if getattr(self, "_repo_map_cache_key", None) != cache_key:
            if hasattr(self, "_repo_map_cache"):
                del self._repo_map_cache
            self._repo_map_cache_key = cache_key
        log.log_task_start(task)
        logger.info("Agent starting task %s", task.task_id)

        # 初始化上下文管理器
        # 如果调用方（ChatSession）注入了共享 history，直接复用；
        # 否则新建（单次 run 模式）
        if hasattr(self, "_pending_history") and self._pending_history is not None:
            history = self._pending_history
        else:
            history = ConversationHistory(max_messages=self._cfg.history_max_messages)
            # 单次模式：把任务描述作为第一条 user 消息
            from agent.prompt import build_task_prompt
            history.add(LLMMessage(
                role="user",
                content=build_task_prompt(task.description, task.repo_path, task.issue_url),
            ))
        token_budget = TokenBudget(total=self._cfg.budget_tokens)
        repo_map = RepoMap(task.repo_path)

        total_tokens = 0
        steps_without_edit = 0

        for step in range(1, task.max_steps + 1):
            logger.debug("Step %d/%d", step, task.max_steps)

            # ── 1. 组装 messages，调用 LLM ──────────────────────────────
            messages = self._build_messages(history, token_budget, repo_map)
            tools = self._registry.get_schemas()

            try:
                response = self._call_with_retry(messages, tools)
            except Exception as exc:
                logger.error("LLM call failed at step %d after retries: %s", step, exc)
                log.log_task_failed(steps=step, reason=f"LLM error: {exc}")
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.FAILED,
                    summary=f"LLM call failed: {exc}",
                    steps_taken=step,
                    total_tokens=total_tokens,
                    error=str(exc),
                )

            total_tokens += response.total_tokens
            action = response.action

            # ── 2. 写入 Action event ────────────────────────────────────
            log.log_action(step=step, action=action, raw_content=response.raw_content)
            logger.info("Step %d: %r", step, action)

            # ── 3. 检测死循环（连续相同 action）────────────────────────
            if self._is_looping(log):
                reason = f"Loop detected: same action repeated {self._cfg.loop_detection_window} times"
                logger.warning(reason)
                log.log_task_failed(steps=step, reason=reason)
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.GAVE_UP,
                    summary=reason,
                    steps_taken=step,
                    total_tokens=total_tokens,
                )

            # ── 4. 终止 action ──────────────────────────────────────────
            if action.action_type == ActionType.FINISH:
                summary = action.message or "Task complete."
                patch = self._get_git_diff(task.repo_path)
                log.log_task_complete(steps=step, summary=summary)
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.SUCCESS,
                    summary=summary,
                    steps_taken=step,
                    total_tokens=total_tokens,
                    patch=patch,
                )

            if action.action_type == ActionType.GIVE_UP:
                reason = action.message or "Agent gave up."
                log.log_task_failed(steps=step, reason=reason)
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.GAVE_UP,
                    summary=reason,
                    steps_taken=step,
                    total_tokens=total_tokens,
                )

            # ── 5. 执行工具 ─────────────────────────────────────────────
            if action.action_type == ActionType.TOOL_CALL and action.tool_call:
                tc = action.tool_call
                result = self._registry.execute_tool(tc.name, tc.params)
                observation = result.to_observation(tc.name)

                # 追踪是否有文件写操作
                if tc.name in ("file_write", "file_edit", "edit"):
                    steps_without_edit = 0
                else:
                    steps_without_edit += 1

                log.log_observation(step=step, observation=observation)

                # 把 action 和 observation 加入对话历史
                history.add(LLMMessage(
                    role="assistant",
                    content=self._format_action_for_history(action),
                ))
                history.add(LLMMessage(
                    role="user",
                    content=self._format_observation_for_history(observation),
                ))

                # ── 6. Reflection 触发判断 ──────────────────────────────

                # 触发条件 A：测试工具失败
                if (
                    tc.name in self._cfg.test_tool_names
                    and not observation.is_success()
                ):
                    reflect_prompt = reflection_test_failed()
                    log.log_reflection(
                        step=step,
                        reason="test_failed",
                        prompt=reflect_prompt,
                    )
                    history.add(LLMMessage(role="user", content=reflect_prompt))
                    logger.debug("Reflection triggered: test_failed at step %d", step)

                # 触发条件 B：连续 N 步无编辑
                elif steps_without_edit >= self._cfg.reflection_no_edit_steps:
                    reflect_prompt = reflection_no_edit(steps_without_edit)
                    log.log_reflection(
                        step=step,
                        reason="no_edit",
                        prompt=reflect_prompt,
                    )
                    history.add(LLMMessage(role="user", content=reflect_prompt))
                    steps_without_edit = 0  # 重置计数，避免每步都触发
                    logger.debug("Reflection triggered: no_edit at step %d", step)

            elif action.action_type == ActionType.REFLECTION:
                # LLM 主动要求 reflection（预留，当前 MockBackend 不产生）
                history.add(LLMMessage(
                    role="assistant",
                    content=action.thought,
                ))

        # ── 7. 超出步数上限 ─────────────────────────────────────────────
        reason = f"Reached max_steps limit ({task.max_steps})"
        log.log_task_failed(steps=task.max_steps, reason=reason)
        return RunResult(
            task_id=task.task_id,
            status=RunStatus.MAX_STEPS,
            summary=reason,
            steps_taken=task.max_steps,
            total_tokens=total_tokens,
        )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _build_messages(
        self,
        history: ConversationHistory,
        token_budget: TokenBudget,
        repo_map: RepoMap,
    ) -> list[LLMMessage]:
        """
        组装发给 LLM 的完整 messages，含 token 裁剪。
        """
        schemas = self._registry.get_schemas()

        # 生成 repo-map（带缓存：只在第一步生成，之后复用）
        if not hasattr(self, "_repo_map_cache"):
            self._repo_map_cache = repo_map.build(
                budget=token_budget.default_plan().repo_map
            )

        system_content = build_system_prompt(
            repo_path=getattr(self, "_current_repo_path", "."),
            tools=schemas,
            repo_summary=self._repo_map_cache,
        )

        # 裁剪历史
        trimmed_history_dicts = token_budget.trim_history(
            history.to_dicts(),
            token_budget.default_plan().history,
        )

        # 组装：system + 裁剪后的 history
        messages = [LLMMessage(role="system", content=system_content)]
        for d in trimmed_history_dicts:
            messages.append(LLMMessage(role=d["role"], content=d["content"]))
        return messages

    def _format_action_for_history(self, action: Action) -> str:
        """把 Action 格式化为 assistant 消息，写入对话历史。"""
        parts = [f"Thought: {action.thought}"]
        if action.tool_call:
            parts.append(f"Action: {action.tool_call.name}")
            parts.append(f"Params: {json.dumps(action.tool_call.params, ensure_ascii=False)}")
        elif action.message:
            parts.append(f"Message: {action.message}")
        return "\n".join(parts)

    def _format_observation_for_history(self, observation: Observation) -> str:
        """把 Observation 格式化为 user 消息，写入对话历史。"""
        status = "SUCCESS" if observation.is_success() else "ERROR"
        lines = [f"[Tool: {observation.tool_name} | {status}]"]
        if observation.output:
            lines.append(observation.output)
        if observation.error and not observation.is_success():
            lines.append(f"Error: {observation.error}")
        return "\n".join(lines)

    def _is_looping(self, log: EventLog) -> bool:
        """
        检测是否陷入死循环：最近 N 条 action 完全相同。
        比较 (tool_name, params) 元组。
        """
        n = self._cfg.loop_detection_window
        actions = log.get_actions()
        if len(actions) < n:
            return False

        recent = actions[-n:]
        # 只对 TOOL_CALL 类型做检测
        if not all(a.action_type == ActionType.TOOL_CALL for a in recent):
            return False
        if not all(a.tool_call for a in recent):
            return False

        first = recent[0].tool_call
        return all(
            a.tool_call.name == first.name and a.tool_call.params == first.params
            for a in recent[1:]
        )

    def _call_with_retry(
        self,
        messages: list[LLMMessage],
        tools: list[LLMToolSchema],
    ):
        """
        带指数退避重试的 LLM 调用。
        stream=True 时走 backend.stream()，否则走 complete()。
        不重试：认证失败（401/403）、参数错误（400）。
        """
        import time as _time

        last_exc: Exception | None = None
        delay = self._cfg.llm_retry_delay

        for attempt in range(1, self._cfg.llm_max_retries + 1):
            try:
                if self._cfg.stream:
                    cb = self._cfg.stream_callback
                    thought_cb = self._cfg.thought_callback
                    if hasattr(self._backend, "stream"):
                        return self._backend.stream(
                            messages, tools,
                            on_text=cb,
                            on_thought=thought_cb,
                        )
                return self._backend.complete(messages, tools)
            except Exception as exc:
                last_exc = exc
                exc_str = str(exc).lower()
                if any(kw in exc_str for kw in (
                    "401", "403", "invalid api key", "authentication",
                    "400", "bad request",
                )):
                    raise
                if attempt < self._cfg.llm_max_retries:
                    logger.warning(
                        "LLM call failed (attempt %d/%d): %s — retrying in %.1fs",
                        attempt, self._cfg.llm_max_retries, exc, delay,
                    )
                    _time.sleep(delay)
                    delay *= 2

        raise last_exc  # type: ignore[misc]

    def _get_git_diff(self, repo_path: str) -> str | None:
        """抓取 git diff HEAD 作为 patch，失败时静默返回 None。"""
        return _git_diff(repo_path)


# ---------------------------------------------------------------------------
# 向后兼容别名 — 所有旧代码可继续使用 Agent
# ---------------------------------------------------------------------------

Agent = ReActAgent


# ---------------------------------------------------------------------------
# PlanExecuteAgent — Claude Code 风格 Plan-then-Execute
# ---------------------------------------------------------------------------

# 只读工具白名单（Phase 1 规划阶段可用）
_READONLY_TOOLS = frozenset({
    "file_read", "file_view", "find_files", "find_symbol",
    "search_text", "git_status", "git_diff",
    "web_search", "web_fetch",
})


class PlanExecuteAgent:
    """
    Claude Code 风格的 Plan-then-Execute Agent。

    两阶段同上下文设计：
    - Phase 1（规划）: 只读探索 → 生成 markdown 计划 → 用户审批
    - Phase 2（执行）: 全工具权限 → 在同一对话上下文中执行计划

    与旧设计的区别：
    - 规划阶段有工具访问权（只读），可以实际探索代码
    - 计划是 markdown（人类可读），不是 JSON subtask 列表
    - 需要用户审批后才执行
    - 执行阶段保留规划阶段的对话历史（不丢失探索上下文）

    用法：
        agent = PlanExecuteAgent(backend, registry, config, plan_config)
        result = agent.run(task, log)
    """

    def __init__(
        self,
        backend: LLMBackend,
        registry: ToolRegistry,
        agent_config: AgentConfig | None = None,
        plan_config: "PlanExecuteConfig | None" = None,
        planning_backend: LLMBackend | None = None,
    ) -> None:
        self._backend = backend
        self._planning_backend = planning_backend or backend
        self._registry = registry
        self._cfg = agent_config or AgentConfig()
        from agent.plan import PlanExecuteConfig as _PlanCfg
        self._plan_cfg = plan_config or _PlanCfg()
        self._react_agent = ReActAgent(backend, registry, self._cfg)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def run(self, task: Task, log: EventLog) -> RunResult:
        """
        两阶段 Plan-then-Execute。

        Phase 1: 只读探索 + 计划生成
        Phase 2: 用户审批后全权执行
        """
        from agent.plan import Plan
        from agent.prompt import get_plan_mode_injection, get_plan_execution_injection

        log.log_task_start(task)
        logger.info("PlanExecuteAgent starting task %s", task.task_id)

        # ── Phase 1: 只读规划 ──────────────────────────────────────
        plan_steps = max(5, task.max_steps // 3)
        plan_task = Task(
            description=task.description,
            repo_path=task.repo_path,
            max_steps=plan_steps,
            budget_tokens=task.budget_tokens // 3,
        )

        readonly_registry = self._make_readonly_registry()
        plan_agent = ReActAgent(self._backend, readonly_registry, self._cfg)

        plan_history = ConversationHistory(
            max_messages=self._cfg.history_max_messages,
        )
        plan_injection = get_plan_mode_injection()
        plan_history.add(LLMMessage(
            role="user",
            content=(
                f"{plan_injection}\n\n"
                f"## Repository\n{task.repo_path}\n\n"
                f"## Task\n{task.description}\n\n"
                f"Explore the codebase and produce an implementation plan. "
                f"Call finish with your plan when ready."
            ),
        ))
        plan_agent._pending_history = plan_history

        plan_log = EventLog.create(plan_task, log_dir=self._plan_cfg.plan_subtask_log_dir)
        plan_result = plan_agent.run(plan_task, plan_log)
        plan_log.close()

        if hasattr(plan_agent, "_pending_history"):
            del plan_agent._pending_history

        # 提取 plan 文本
        plan_text = plan_result.summary or ""
        if not plan_text.strip():
            logger.warning("Plan generation produced empty result — falling back")
            return self._react_agent.run(task, log)

        plan = Plan.from_markdown(plan_text, task.description)
        log.log_plan_generated(plan)
        logger.info("Plan generated (%d chars): %s...", len(plan_text), plan_text[:100])

        # ── 用户审批 ──────────────────────────────────────────────
        approval_cb = self._plan_cfg.plan_approval_callback
        if approval_cb:
            approved = approval_cb(plan_text)
            if not approved:
                reason = "Plan rejected by user"
                log.log_task_failed(steps=plan_result.steps_taken, reason=reason)
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.GAVE_UP,
                    summary=reason,
                    steps_taken=plan_result.steps_taken,
                    total_tokens=plan_result.total_tokens,
                )

        # ── Phase 2: 执行 ─────────────────────────────────────────
        exec_steps = task.max_steps - plan_result.steps_taken
        if exec_steps < 3:
            exec_steps = 3

        exec_task = Task(
            description=task.description,
            repo_path=task.repo_path,
            max_steps=exec_steps,
            budget_tokens=task.budget_tokens - plan_result.total_tokens,
        )

        exec_history = ConversationHistory(
            max_messages=self._cfg.history_max_messages,
        )
        # 注入执行上下文：携带 plan 内容
        exec_injection = get_plan_execution_injection()
        exec_history.add(LLMMessage(
            role="user",
            content=(
                f"Repository: {task.repo_path}\n\n"
                f"## Original Task\n{task.description}\n\n"
                f"## Your Approved Plan\n{plan_text}\n\n"
                f"{exec_injection}"
            ),
        ))

        self._react_agent._pending_history = exec_history
        exec_result = self._react_agent.run(exec_task, log)

        if hasattr(self._react_agent, "_pending_history"):
            del self._react_agent._pending_history

        total_tokens = plan_result.total_tokens + exec_result.total_tokens
        total_steps = plan_result.steps_taken + exec_result.steps_taken

        if exec_result.is_success():
            patch = _git_diff(task.repo_path)
            summary = exec_result.summary or "Plan executed successfully"
            log.log_task_complete(steps=total_steps, summary=summary)
            return RunResult(
                task_id=task.task_id,
                status=RunStatus.SUCCESS,
                summary=summary,
                steps_taken=total_steps,
                total_tokens=total_tokens,
                patch=patch,
            )

        log.log_task_failed(steps=total_steps, reason=exec_result.summary)
        return RunResult(
            task_id=task.task_id,
            status=exec_result.status,
            summary=exec_result.summary,
            steps_taken=total_steps,
            total_tokens=total_tokens,
            error=exec_result.error,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _make_readonly_registry(self) -> ToolRegistry:
        """构造只包含只读工具的注册表（Phase 1 用）。"""
        readonly = ToolRegistry()
        for name, tool in self._registry._tools.items():
            if name in _READONLY_TOOLS:
                readonly._tools[name] = tool
        return readonly

    def _get_git_diff(self, repo_path: str) -> str | None:
        return _git_diff(repo_path)