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
from typing import TYPE_CHECKING

from agent.event_log import EventLog
from context.history import ConversationHistory
from context.repo_map import RepoMap
from context.token_budget import TokenBudget
from agent.prompt import (
    build_system_prompt,
    build_system_prompt_structured,
    build_task_prompt,
    reflection_no_edit,
    reflection_test_failed,
)
from agent.task import (
    Action, ActionType, Event, EventType,
    Observation, ObservationStatus, RunResult, RunStatus, Task, ToolCall,
)
from context.compaction import ConversationCompactor
from llm.base import LLMBackend, LLMMessage, LLMToolSchema
from tools.base import ToolRegistry

if TYPE_CHECKING:
    from memory.context import MemoryContext

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
            encoding="utf-8", errors="replace",
        )
        diff = proc.stdout.strip()
        return diff if diff else None
    except Exception:
        return None


# 只读工具白名单（Plan Mode 下可用）
_READONLY_TOOLS = frozenset({
    "file_read", "file_view", "find_files", "find_symbol",
    "search_text", "git_status", "git_diff",
    "web_search", "web_fetch",
    "memory_read", "memory_list",
})


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
        memory_context: "MemoryContext | None" = None,
    ) -> None:
        self._backend = backend
        self._full_registry = registry  # 保存完整注册表
        self._registry = registry
        self._readonly_registry = self._build_readonly_registry()
        self._cfg = config or AgentConfig()
        self._memory_context = memory_context

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

        # 设置任务上下文，用于记忆相关性过滤
        if self._memory_context:
            self._memory_context.set_task_context(task.description)

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
            self._current_step = step  # 用于 compaction 日志
            logger.debug("Step %d/%d", step, task.max_steps)

            # ── 1. 组装 messages，调用 LLM ──────────────────────────────
            # 设置最新用户消息，供 RAG 主动检索使用
            if self._memory_context:
                last_user_msg = history.get_last_user_message()
                if last_user_msg:
                    self._memory_context.set_user_message(last_user_msg)

            messages = self._build_messages(
                history, token_budget, repo_map,
                consumed_tokens=total_tokens,
                max_context_window=self._backend.max_context_window,
            )
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

            # ── 5. 执行工具（支持并行 tool_calls）───────────────────────
            if action.action_type == ActionType.TOOL_CALL and action.tool_calls:
                observations: list[Observation] = []
                any_test_failed = False
                any_edit = False

                for tc in action.tool_calls:
                    result = self._registry.execute_tool(tc.name, tc.params)
                    observation = result.to_observation(tc.name)
                    observations.append(observation)

                    # 追踪是否有文件写操作
                    if tc.name in ("file_write", "file_edit", "edit"):
                        any_edit = True

                    # 追踪测试是否失败
                    if tc.name in self._cfg.test_tool_names and not observation.is_success():
                        any_test_failed = True

                    log.log_observation(step=step, observation=observation)

                # 更新 steps_without_edit（整体判断）
                if any_edit:
                    steps_without_edit = 0
                else:
                    steps_without_edit += 1

                # 把 action 和所有 observations 加入对话历史
                if self._backend.supports_function_calling:
                    # Native tool_use mode: structured messages
                    history.add(LLMMessage(
                        role="assistant",
                        content=action.thought or "",
                        tool_calls=action.tool_calls,
                    ))
                    for i, obs in enumerate(observations):
                        tc = action.tool_calls[i] if i < len(action.tool_calls) else None
                        history.add(LLMMessage(
                            role="tool",
                            content=self._build_tool_result_content(obs),
                            tool_call_id=tc.id if tc else None,
                        ))
                else:
                    # Text fallback mode (e.g. DeepSeek R1)
                    history.add(LLMMessage(
                        role="assistant",
                        content=self._format_action_for_history(action),
                    ))
                    history.add(LLMMessage(
                        role="user",
                        content=self._format_observations_for_history(observations),
                    ))

                # ── 6. Reflection 触发判断 ──────────────────────────────

                # 触发条件 A：任一测试工具失败
                if any_test_failed:
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
        consumed_tokens: int = 0,
        max_context_window: int | None = None,
    ) -> list[LLMMessage]:
        """
        组装发给 LLM 的完整 messages，含 token 裁剪。

        消息结构：
        [system] — 稳定 prompt（core 部分加 cache_control + auto_memory 指导）
        [user: project context] — 记忆索引 + 项目规则（变动不影响 system cache）
        [user/assistant...] — 对话历史

        Args:
            consumed_tokens: 本轮之前已消耗的 token 总数，用于衰减历史配额
            max_context_window: 模型上下文窗口，影响有效预算计算
        """
        schemas = self._registry.get_schemas()

        # 计算本轮的动态配额
        plan = token_budget.compute_plan(
            consumed_tokens=consumed_tokens,
            max_context_window=max_context_window,
        )

        # 生成 repo-map（带缓存：只在第一步生成，之后复用）
        if not hasattr(self, "_repo_map_cache"):
            self._repo_map_cache = repo_map.build(
                budget=plan.repo_map,
            )

        # System prompt: Anthropic 用 structured format（带 cache_control），其他用纯字符串
        enable_caching = self._is_anthropic_backend()
        system_content = build_system_prompt_structured(
            repo_path=getattr(self, "_current_repo_path", "."),
            tools=schemas,
            repo_summary=self._repo_map_cache,
            memory_section="",
            auto_memory_enabled=bool(self._memory_context and self._memory_context.enabled),
            enable_caching=enable_caching,
        )

        # Layer 2: Snip — 移除低价值轮次（零成本）
        history_dicts = history.to_dicts()
        from context.compaction import snip_low_value_turns
        history_dicts = snip_low_value_turns(history_dicts)

        # Layer 3: 滑动窗口裁剪
        from context.compaction import trim_sliding_window
        history_dicts = trim_sliding_window(
            history_dicts,
            token_limit=plan.history,
            keep_recent=3,
        )

        # Layer 4: 检查是否需要 compaction
        if self._should_compact(history_dicts, plan.history):
            compacted_dicts = self._compact_history_from_dicts(history_dicts)
            history_dicts = compacted_dicts
            logger.info("Auto-compaction triggered at step %d", self._current_step)

        # 最后的 token 配额检查兜底
        trimmed_history_dicts = token_budget.trim_history(
            history_dicts,
            plan.history,
        )

        # 组装：system + project context + 裁剪后的 history
        messages = [LLMMessage(role="system", content=system_content)]

        # Project context message: 记忆索引 + 项目规则（独立于 system，不影响 cache）
        project_context = self._build_project_context()
        if project_context:
            messages.append(LLMMessage(role="user", content=project_context))
            messages.append(LLMMessage(role="assistant", content="Understood. I have the project context and memory index. Proceeding with the task."))

        for d in trimmed_history_dicts:
            tool_calls = None
            if "tool_calls" in d:
                tool_calls = [
                    ToolCall(name=tc["name"], params=tc["params"], id=tc.get("id"))
                    for tc in d["tool_calls"]
                ]
            messages.append(LLMMessage(
                role=d["role"],
                content=d["content"],
                tool_call_id=d.get("tool_call_id"),
                tool_calls=tool_calls,
            ))
        return messages

    def _is_anthropic_backend(self) -> bool:
        """判断当前 backend 是否为 Anthropic（支持 prompt cache）。"""
        backend_type = type(self._backend).__name__
        return "anthropic" in backend_type.lower()

    def _build_project_context(self) -> str:
        """
        构建项目上下文消息（记忆索引 + 项目规则）。
        独立于 system prompt，变动不影响 prompt cache。
        """
        parts: list[str] = []

        # 记忆索引
        if self._memory_context and self._memory_context.enabled:
            memory_section = self._memory_context.build_memory_section()
            if memory_section:
                parts.append(memory_section)

        # 项目规则文件
        rules_content = self._load_project_rules()
        if rules_content:
            parts.append(f"## Project Rules\n{rules_content}")

        if not parts:
            return ""

        return "[Project Context — memory index and project rules]\n\n" + "\n\n".join(parts)

    def _load_project_rules(self) -> str:
        """加载项目规则文件（.forge-agent/rules.md），不存在时返回空字符串。"""
        import os
        repo_path = getattr(self, "_current_repo_path", ".")
        rules_path = os.path.join(repo_path, ".forge-agent", "rules.md")
        try:
            if os.path.isfile(rules_path):
                with open(rules_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    logger.debug("Loaded project rules from %s", rules_path)
                    return content
        except OSError as exc:
            logger.debug("Failed to load project rules: %s", exc)
        return ""

    def _format_action_for_history(self, action: Action) -> str:
        """把 Action 格式化为 assistant 消息，写入对话历史。支持并行 tool_calls。"""
        parts = [f"Thought: {action.thought}"]
        if action.tool_calls:
            for tc in action.tool_calls:
                parts.append(f"Action: {tc.name}")
                parts.append(f"Params: {json.dumps(tc.params, ensure_ascii=False)}")
        elif action.message:
            parts.append(f"Message: {action.message}")
        return "\n".join(parts)

    def _format_observation_for_history(self, observation: Observation) -> str:
        """把单条 Observation 格式化为 user 消息（text fallback mode）。"""
        status = "SUCCESS" if observation.is_success() else "ERROR"
        lines = [f"[Tool: {observation.tool_name} | {status}]"]
        if observation.output:
            lines.append(observation.output)
        if observation.error and not observation.is_success():
            lines.append(f"Error: {observation.error}")
        return "\n".join(lines)

    def _build_tool_result_content(self, observation: Observation) -> str:
        """构建 native tool_use 模式下的工具结果内容（不含 [Tool:] 包装）。"""
        parts: list[str] = []
        if observation.output:
            parts.append(observation.output)
        if observation.error and not observation.is_success():
            parts.append(f"Error: {observation.error}")
        return "\n".join(parts) if parts else "(no output)"

    def _format_observations_for_history(self, observations: list[Observation]) -> str:
        """把多条 Observation 格式化为一条 user 消息（并行 tool_calls 用，text fallback mode）。"""
        lines = []
        for obs in observations:
            status = "SUCCESS" if obs.is_success() else "ERROR"
            lines.append(f"[Tool: {obs.tool_name} | {status}]")
            if obs.output:
                lines.append(obs.output)
            if obs.error and not obs.is_success():
                lines.append(f"Error: {obs.error}")
        return "\n".join(lines)

    def _is_looping(self, log: EventLog) -> bool:
        """
        检测是否陷入死循环：最近 N 条 action 完全相同。
        比较所有 tool_calls 的 (name, params) 序列。
        """
        n = self._cfg.loop_detection_window
        actions = log.get_actions()
        if len(actions) < n:
            return False

        recent = actions[-n:]
        # 只对 TOOL_CALL 类型做检测
        if not all(a.action_type == ActionType.TOOL_CALL for a in recent):
            return False
        if not all(a.tool_calls for a in recent):
            return False

        def _serialize(action: Action) -> tuple:
            """把所有 tool_calls 序列化为可比较的元组。"""
            return tuple(
                (tc.name, tuple(sorted(tc.params.items())))
                for tc in action.tool_calls
            )

        first = _serialize(recent[0])
        return all(_serialize(a) == first for a in recent[1:])

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

    # ------------------------------------------------------------------
    # 权限模式切换（Plan Mode / Execute Mode）
    # ------------------------------------------------------------------

    def switch_to_plan_mode(self) -> None:
        """切换到规划模式（只读工具）。"""
        self._registry = self._readonly_registry
        logger.info("Switched to plan mode (readonly tools)")

    def switch_to_execute_mode(self) -> None:
        """切换到执行模式（完整工具）。"""
        self._registry = self._full_registry
        logger.info("Switched to execute mode (full tools)")

    def _build_readonly_registry(self) -> ToolRegistry:
        """从完整注册表构建只读版本（仅含 _READONLY_TOOLS 白名单中的工具）。"""
        from tools.base import ToolRegistry
        readonly = ToolRegistry()
        for name, tool in self._full_registry._tools.items():
            if name in _READONLY_TOOLS:
                readonly._tools[name] = tool
        return readonly

    # ------------------------------------------------------------------
    # Compaction（对话压缩）
    # ------------------------------------------------------------------

    _compactor: ConversationCompactor | None = None

    @property
    def compactor(self) -> ConversationCompactor:
        if self._compactor is None:
            self._compactor = ConversationCompactor(backend=self._backend)
        return self._compactor

    def _should_compact(self, history_dicts: list[dict], history_budget: int) -> bool:
        """判断是否需要自动 compaction。"""
        return self.compactor.should_compact(history_dicts, history_budget)

    def _compact_history_from_dicts(self, history_dicts: list[dict]) -> list[dict]:
        """执行 compaction，返回压缩后的 dict 列表。"""
        compacted = self.compactor.compact_history(history_dicts)
        logger.info(
            "Compaction: %d messages → %d messages",
            len(history_dicts), len(compacted),
        )
        return compacted

    def _compact_history(self, history: ConversationHistory) -> ConversationHistory:
        """执行 compaction，返回新的 ConversationHistory（保持向后兼容）。"""
        compacted_dicts = self._compact_history_from_dicts(history.to_dicts())
        new_history = ConversationHistory(max_messages=self._cfg.history_max_messages)
        for d in compacted_dicts:
            tool_calls = None
            if "tool_calls" in d:
                tool_calls = [
                    ToolCall(name=tc["name"], params=tc["params"], id=tc.get("id"))
                    for tc in d["tool_calls"]
                ]
            new_history.add(LLMMessage(
                role=d["role"],
                content=d["content"],
                tool_call_id=d.get("tool_call_id"),
                tool_calls=tool_calls,
            ))
        logger.info(
            "Compaction: %d messages → %d messages",
            len(history), len(new_history),
        )

        # 持久化 session summary 到磁盘（供跨 session 恢复）
        if self._memory_context and hasattr(self._memory_context, "_store"):
            from context.compaction import persist_compaction_summary
            summary_text = compacted_dicts[0]["content"] if compacted_dicts else ""
            if summary_text:
                store_dir = str(self._memory_context._store.store_dir.parent)
                persist_compaction_summary(summary_text, store_dir)

        return new_history


# ---------------------------------------------------------------------------
# 向后兼容别名 — 所有旧代码可继续使用 Agent
# ---------------------------------------------------------------------------

Agent = ReActAgent


# ---------------------------------------------------------------------------
# PlanExecuteAgent — Claude Code 风格 Plan-then-Execute
# ---------------------------------------------------------------------------


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
        memory_context: "MemoryContext | None" = None,
    ) -> None:
        self._backend = backend
        self._planning_backend = planning_backend or backend
        self._registry = registry
        self._cfg = agent_config or AgentConfig()
        self._memory_context = memory_context
        from agent.plan import PlanExecuteConfig as _PlanCfg
        self._plan_cfg = plan_config or _PlanCfg()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def run(self, task: Task, log: EventLog) -> RunResult:
        """
        两阶段 Plan-then-Execute（同实例切换权限）。

        Phase 1: 只读探索 → 生成 markdown 计划
        Phase 2: 用户审批后全权执行

        与旧设计的区别：
        - 同一个 ReActAgent 实例，同一个 ConversationHistory
        - Phase 1 的探索上下文在 Phase 2 完全保留
        - 通过 switch_to_plan_mode() / switch_to_execute_mode() 切换权限
        """
        from agent.plan import Plan
        from agent.prompt import get_plan_mode_injection, get_plan_execution_injection

        log.log_task_start(task)
        logger.info("PlanExecuteAgent starting task %s", task.task_id)

        # ── 创建同一个 agent + 同一个 history ──────────────────
        agent = ReActAgent(
            self._backend, self._registry, self._cfg,
            memory_context=self._memory_context,
        )
        history = ConversationHistory(
            max_messages=self._cfg.history_max_messages,
        )

        plan_injection = get_plan_mode_injection()
        history.add(LLMMessage(
            role="user",
            content=(
                f"{plan_injection}\n\n"
                f"## Repository\n{task.repo_path}\n\n"
                f"## Task\n{task.description}\n\n"
                f"Explore the codebase and produce an implementation plan. "
                f"Call finish with your plan when ready."
            ),
        ))
        agent._pending_history = history

        # ── Phase 1: 只读规划（切换到只读注册表）─────────────
        plan_steps = max(5, task.max_steps // 3)
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

        # 提取 plan 文本
        plan_text = plan_result.summary or ""
        if not plan_text.strip():
            logger.warning("Plan generation produced empty result — falling back")
            agent.switch_to_execute_mode()
            fallback_task = Task(
                description=task.description,
                repo_path=task.repo_path,
                max_steps=task.max_steps,
                budget_tokens=task.budget_tokens,
            )
            return agent.run(fallback_task, log)

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

        # ── Phase 2: 执行（切换到完整注册表，复用 history）───
        exec_steps = task.max_steps - plan_result.steps_taken
        if exec_steps < 3:
            exec_steps = 3

        exec_task = Task(
            description=task.description,
            repo_path=task.repo_path,
            max_steps=exec_steps,
            budget_tokens=task.budget_tokens - plan_result.total_tokens,
        )

        # 在同一个 history 中追加 plan 确认和执行指令
        exec_injection = get_plan_execution_injection()
        history.add(LLMMessage(
            role="user",
            content=(
                f"## Your Approved Plan\n{plan_text}\n\n"
                f"{exec_injection}"
            ),
        ))

        agent.switch_to_execute_mode()
        exec_result = agent.run(exec_task, log)

        # ── 清理 ──────────────────────────────────────────────────
        if hasattr(agent, "_pending_history"):
            del agent._pending_history

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

    def _get_git_diff(self, repo_path: str) -> str | None:
        return _git_diff(repo_path)