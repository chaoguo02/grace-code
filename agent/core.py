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
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agent.event_log import EventLog
from context.history import ConversationHistory
from context.repo_map import RepoMap
from context.token_budget import TokenBudget
from agent.prompt import (
    build_system_prompt,
    build_system_prompt_core,
    build_system_prompt_variable,
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
from tools.base import ToolRegistry, ToolResult

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
    missing_test_target_max_followups: int = 2  # pytest 路径缺失后最多允许的确认性探索步数
    history_max_messages: int = 40         # 历史最大条数
    llm_max_retries: int = 3               # LLM 调用失败最大重试次数
    llm_retry_delay: float = 2.0           # 重试间隔（秒，指数退避）
    stream: bool = False                   # 是否启用流式输出
    stream_callback: object = None         # StreamCallback，最终回答流式回调
    thought_callback: object = None        # StreamCallback，推理过程流式回调（推理模型专用）
    confirm_dangerous: bool = False        # 是否对危险命令要求用户确认
    confirm_callback: object = None        # ConfirmCallback，None=跳过确认
    compact_history: bool = True           # 是否启用积极的历史压缩（sub-agent 应关闭）



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

_PATH_CONSTRAINT_RE = re.compile(
    r"(?:只允许|仅允许|只能|只准|不要查看其他文件|do not (?:read|inspect|view|open) other files|only (?:read|inspect|view|open))"
    r"[^\n。；;]*?"
    r"([A-Za-z0-9_./\\-]+\.[A-Za-z0-9]+|README(?:\.md)?)",
    re.IGNORECASE,
)


def _normalize_allowed_path(path_text: str) -> str:
    normalized = path_text.strip().strip("`'\"，,。.;；:：")
    normalized = normalized.replace("\\", "/")
    if normalized.lower() == "readme":
        normalized = "README.md"
    return normalized.lstrip("./")


def _extract_single_file_constraint(description: str) -> str | None:
    match = _PATH_CONSTRAINT_RE.search(description)
    if not match:
        return None
    return _normalize_allowed_path(match.group(1))


class _SingleFileReadOnlyRegistry(ToolRegistry):
    """Restrict plan/analysis execution to reading one explicitly allowed file."""

    def __init__(self, base: ToolRegistry, allowed_path: str) -> None:
        super().__init__(hitl_manager=getattr(base, "_hitl_manager", None))
        self._base = base
        self._allowed_path = _normalize_allowed_path(allowed_path)
        for tool_name in ("file_read", "file_view"):
            if tool_name in base._tools:
                self._tools[tool_name] = base._tools[tool_name]

    def execute_tool(self, name: str, params: dict[str, Any], thought: str = "") -> ToolResult:
        if name not in {"file_read", "file_view"}:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Tool '{name}' is blocked by the user's single-file constraint. "
                    f"Only file_read/file_view on '{self._allowed_path}' are allowed."
                ),
            )
        requested = _normalize_allowed_path(str(params.get("path", "")))
        if requested != self._allowed_path:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Path '{requested}' is blocked by the user's single-file constraint. "
                    f"Only '{self._allowed_path}' may be read."
                ),
            )
        return self._base.execute_tool(name, params, thought=thought)


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
        self._current_task_description = task.description
        self._task_intent = getattr(task, "intent", "edit")
        # 按 repo_path 隔离 repo_map 缓存，换 repo 时自动重建
        cache_key = task.repo_path
        if getattr(self, "_repo_map_cache_key", None) != cache_key:
            if hasattr(self, "_repo_map_cache"):
                del self._repo_map_cache
            self._repo_map_cache_key = cache_key

        # 设置任务上下文，用于记忆相关性过滤
        if self._memory_context:
            self._memory_context.set_task_context(task.description)

        self._loop_break_injected = False
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
                content=build_task_prompt(
                    task.description, task.repo_path, task.issue_url,
                    intent=self._task_intent,
                ),
            ))
        token_budget = TokenBudget(total=self._cfg.budget_tokens)
        repo_map = RepoMap(task.repo_path)

        total_tokens = 0
        steps_without_edit = 0
        consecutive_failures = 0
        _max_consecutive_failures = 3
        reflection_counts: dict[str, int] = {}  # reason -> count
        missing_test_target_followups: int | None = None
        missing_test_target_message: str | None = None
        missing_test_target_detected_step: int | None = None
        # 累计 prompt caching 统计
        from llm.base import CacheStats
        cumulative_cache = CacheStats()

        for step in range(1, task.max_steps + 1):
            self._current_step = step  # 用于 compaction 日志
            self.compactor.tick_step()
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
                    cache_stats=cumulative_cache,
                )

            billable_tokens = response.total_tokens
            if response.cache_stats and response.cache_stats.has_cache_activity:
                cumulative_cache.cache_read_tokens += response.cache_stats.cache_read_tokens
                cumulative_cache.cache_creation_tokens += response.cache_stats.cache_creation_tokens
                # Cached prompt tokens still appear in provider usage, but they should not
                # trip this run's hard exploration budget on repeated short tasks.
                billable_tokens = max(0, billable_tokens - response.cache_stats.cache_read_tokens)
            total_tokens += billable_tokens

            # ── Token budget 硬上限 ────────────────────────────────────
            if total_tokens > self._cfg.budget_tokens:
                reason = (
                    f"Token budget exceeded: {total_tokens} > {self._cfg.budget_tokens}. "
                    f"Stopping to prevent unbounded cost."
                )
                logger.warning(reason)
                log.log_task_failed(steps=step, reason=reason)
                return RunResult(
                    task_id=task.task_id, status=RunStatus.GAVE_UP,
                    summary=reason, steps_taken=step,
                    total_tokens=total_tokens,
                    cache_stats=cumulative_cache,
                )

            action = response.action

            # ── 2. 写入 Action event ────────────────────────────────────
            log.log_action(step=step, action=action, raw_content=response.raw_content)
            logger.info("Step %d: %r", step, action)

            # ── 3. 检测死循环（连续相同 action）────────────────────────
            if self._is_looping(log):
                if not getattr(self, "_loop_break_injected", False):
                    # 第一次检测到循环：注入反射消息，让 LLM 停止重复
                    self._loop_break_injected = True
                    logger.warning("Loop detected — injecting break reflection")
                    history.add(LLMMessage(
                        role="user",
                        content=(
                            "[SYSTEM] You are repeating the same action. STOP repeating. "
                            "You already have the information you need. "
                            "Produce your final answer NOW using the finish action.\n\n"
                            f"[TASK ANCHOR] Your current task is: {task.description}"
                        ),
                    ))
                    continue
                reason = f"Loop detected: same action repeated {self._cfg.loop_detection_window} times"
                logger.warning(reason)
                log.log_task_failed(steps=step, reason=reason)
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.GAVE_UP,
                    summary=reason,
                    steps_taken=step,
                    total_tokens=total_tokens,
                    cache_stats=cumulative_cache,
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
                    cache_stats=cumulative_cache,
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
                    cache_stats=cumulative_cache,
                )

            # ── 5. 执行工具（支持并行 tool_calls）───────────────────────
            if action.action_type == ActionType.TOOL_CALL and action.tool_calls:
                observations: list[Observation] = []
                any_test_failed = False
                missing_test_target_observation: Observation | None = None
                any_edit = False

                for tc in action.tool_calls:
                    result = self._registry.execute_tool(tc.name, tc.params, thought=action.thought or "")
                    observation = result.to_observation(tc.name)
                    observations.append(observation)

                    # 追踪是否有文件写操作
                    if tc.name in ("file_write", "file_edit", "edit"):
                        any_edit = True

                    # 追踪测试是否失败
                    if tc.name in self._cfg.test_tool_names and not observation.is_success():
                        any_test_failed = True
                        if self._is_missing_test_target_observation(observation):
                            missing_test_target_observation = observation

                    log.log_observation(step=step, observation=observation)

                    if missing_test_target_observation is not None:
                        missing_test_target_message = self._format_missing_test_target_summary(
                            missing_test_target_observation
                        )
                        logger.info("Stopping immediately after missing pytest target")
                        log.log_task_complete(steps=step, summary=missing_test_target_message)
                        return RunResult(
                            task_id=task.task_id,
                            status=RunStatus.SUCCESS,
                            summary=missing_test_target_message,
                            steps_taken=step,
                            total_tokens=total_tokens,
                            cache_stats=cumulative_cache,
                        )

                # 更新 steps_without_edit（整体判断）
                if any_edit:
                    steps_without_edit = 0
                else:
                    steps_without_edit += 1

                # 缺失的指定测试文件是阻塞条件，不是自动创建测试的授权。
                if missing_test_target_observation is not None:
                    if missing_test_target_message is None:
                        missing_test_target_message = self._format_missing_test_target_summary(
                            missing_test_target_observation
                        )
                        missing_test_target_followups = self._cfg.missing_test_target_max_followups
                        missing_test_target_detected_step = step
                    else:
                        missing_test_target_followups = 0

                # 连续失败计数器
                all_failed = all(not obs.is_success() for obs in observations)
                if all_failed:
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0

                # 连续失败超过阈值：强制终止
                if consecutive_failures >= _max_consecutive_failures:
                    reason = (
                        f"Aborting: {consecutive_failures} consecutive tool failures. "
                        f"Last error: {observations[-1].error or observations[-1].output[:200]}"
                    )
                    logger.warning(reason)
                    log.log_task_failed(steps=step, reason=reason)
                    return RunResult(
                        task_id=task.task_id,
                        status=RunStatus.GAVE_UP,
                        summary=reason,
                        steps_taken=step,
                        total_tokens=total_tokens,
                        cache_stats=cumulative_cache,
                    )

                # 把 action 和所有 observations 加入对话历史
                if self._backend.supports_function_calling:
                    # Native tool_use mode: structured messages
                    thought_content = action.thought or ""
                    if thought_content == "(no thought)":
                        thought_content = ""
                    history.add(LLMMessage(
                        role="assistant",
                        content=thought_content,
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

                # 缺失测试目标后，只允许少量确认性搜索，随后强制停止。
                if (
                    missing_test_target_message is not None
                    and missing_test_target_followups is not None
                    and missing_test_target_detected_step != step
                ):
                    if self._is_confirmation_search_action(action):
                        missing_test_target_followups -= 1
                    else:
                        missing_test_target_followups = 0

                    if missing_test_target_followups <= 0:
                        logger.info("Stopping after missing pytest target guardrail")
                        log.log_task_complete(steps=step, summary=missing_test_target_message)
                        return RunResult(
                            task_id=task.task_id,
                            status=RunStatus.SUCCESS,
                            summary=missing_test_target_message,
                            steps_taken=step,
                            total_tokens=total_tokens,
                            cache_stats=cumulative_cache,
                        )

                # ── 6. Reflection 触发判断 ──────────────────────────────

                # Task anchor 用于 reflection，防止 LLM 在反射后丢失当前任务
                _task_anchor = f"\n\n[TASK ANCHOR] Your current task is: {task.description}"

                # 触发条件 A：任一测试工具失败
                if any_test_failed:
                    if missing_test_target_message is not None:
                        reflect_prompt = self._missing_test_target_reflection(missing_test_target_message) + _task_anchor
                        log.log_reflection(
                            step=step,
                            reason="missing_test_target",
                            prompt=reflect_prompt,
                        )
                        history.add(LLMMessage(role="user", content=reflect_prompt))
                        logger.debug("Reflection triggered: missing_test_target at step %d", step)
                        continue

                    reflection_counts["test_failed"] = reflection_counts.get("test_failed", 0) + 1
                    if reflection_counts["test_failed"] >= 3:
                        reason = "Aborting: test failures repeated 3 times without resolution."
                        logger.warning(reason)
                        log.log_task_failed(steps=step, reason=reason)
                        return RunResult(
                            task_id=task.task_id,
                            status=RunStatus.GAVE_UP,
                            summary=reason,
                            steps_taken=step,
                            total_tokens=total_tokens,
                            cache_stats=cumulative_cache,
                        )
                    reflect_prompt = reflection_test_failed() + _task_anchor
                    log.log_reflection(
                        step=step,
                        reason="test_failed",
                        prompt=reflect_prompt,
                    )
                    history.add(LLMMessage(role="user", content=reflect_prompt))
                    logger.debug("Reflection triggered: test_failed at step %d", step)

                # 触发条件 B：连续 N 步无编辑（仅 edit 类型任务触发）
                elif (steps_without_edit >= self._cfg.reflection_no_edit_steps
                      and self._task_intent == "edit"):
                    reflection_counts["no_edit"] = reflection_counts.get("no_edit", 0) + 1
                    if reflection_counts["no_edit"] >= 2:
                        reason = "Aborting: stuck in exploration without making progress."
                        logger.warning(reason)
                        log.log_task_failed(steps=step, reason=reason)
                        return RunResult(
                            task_id=task.task_id,
                            status=RunStatus.GAVE_UP,
                            summary=reason,
                            steps_taken=step,
                            total_tokens=total_tokens,
                            cache_stats=cumulative_cache,
                        )
                    reflect_prompt = reflection_no_edit(steps_without_edit) + _task_anchor
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

        # ── 7. 超出步数上限：从 history 提取已收集的信息作为 summary ────
        summary = self._extract_summary_from_history(history)
        log.log_task_failed(steps=task.max_steps, reason="max_steps")
        return RunResult(
            task_id=task.task_id,
            status=RunStatus.MAX_STEPS,
            summary=summary,
            steps_taken=task.max_steps,
            total_tokens=total_tokens,
            cache_stats=cumulative_cache,
        )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _is_missing_test_target_observation(self, observation: Observation) -> bool:
        """识别 pytest exit code 4 中的缺失目标，避免进入修复代码流程。"""
        text = f"{observation.error or ''}\n{observation.output or ''}".lower()
        return (
            observation.tool_name in self._cfg.test_tool_names
            and "pytest" in text
            and (
                "requested test target is missing" in text
                or "file or directory not found" in text
                or "no such file or directory" in text
            )
        )

    def _format_missing_test_target_summary(self, observation: Observation) -> str:
        requested_path = "(unknown)"
        match = re.search(r"Requested path:\s*(.+)", observation.output)
        if match:
            requested_path = match.group(1).strip()
        return (
            f"`pytest {requested_path}` could not run because the requested test path "
            f"`{requested_path}` does not exist. Pytest reported exit code 4 "
            "(usage error / missing path), so this is not an existing failing test. "
            "I did not modify code or create tests. Please provide the correct test path, "
            "or explicitly ask me to add a new test file."
        )

    def _missing_test_target_reflection(self, summary: str) -> str:
        return (
            "[SYSTEM] Pytest failed because the requested test target is missing.\n"
            "This is a blocker, not permission to create a test file.\n"
            "Do NOT inspect unrelated implementation files or start writing tests/code.\n"
            "You may perform at most targeted confirmation searches for the missing path.\n"
            "Then finish with this conclusion:\n"
            f"{summary}"
        )

    def _is_confirmation_search_action(self, action: Action) -> bool:
        """缺失测试路径后，仅允许查找/搜索类工具做有限确认。"""
        allowed_tools = {"find_files", "search_text", "shell"}
        if action.action_type != ActionType.TOOL_CALL or not action.tool_calls:
            return False
        return all(
            tc.name in allowed_tools and self._is_targeted_confirmation_call(tc)
            for tc in action.tool_calls
        )

    def _is_targeted_confirmation_call(self, tc: ToolCall) -> bool:
        if tc.name in {"find_files", "search_text"}:
            return True
        if tc.name != "shell":
            return False
        cmd = str(tc.params.get("cmd", "")).lower()
        if not any(token in cmd for token in ("test_compaction", "compaction", "tests")):
            return False
        return any(cmd.strip().startswith(prefix) for prefix in ("rg", "find", "dir", "ls", "python -c"))

    def _extract_summary_from_history(self, history: ConversationHistory) -> str:
        """从对话历史中提取有意义的 summary（用于 max_steps 截断时）。

        策略：
        1. 倒序扫描 assistant messages，跳过规划性文本（"Let me", "I'll" 等）
        2. 找到含实质内容的 assistant 消息则返回
        3. 如果所有 assistant 都是规划文本，fallback 到 tool results（最可靠的信息源）
        """
        _PLANNING_PREFIXES = (
            "Let me", "I'll", "I need to", "Now let me", "Good,",
            "Now I'll", "Next,", "OK,", "Alright,", "First,",
        )
        msgs = history.to_dicts()

        # Pass 1: 找实质性 assistant 内容（跳过规划文本）
        for msg in reversed(msgs):
            if msg.get("role") == "assistant":
                content = msg.get("content", "").strip()
                if not content or len(content) <= 20:
                    continue
                if any(content.startswith(p) for p in _PLANNING_PREFIXES):
                    continue
                return content[:2000]

        # Pass 2: 提取 tool results（原始工具返回数据，不依赖模型总结）
        tool_contents = []
        for msg in reversed(msgs):
            if msg.get("role") == "tool":
                content = msg.get("content", "").strip()
                if content and len(content) > 10:
                    tool_contents.append(content[:500])
                    if len(tool_contents) >= 5:
                        break

        if tool_contents:
            combined = "\n---\n".join(reversed(tool_contents))
            return f"Raw tool results (max_steps reached):\n{combined}"[:2000]

        return f"Reached max_steps limit ({len(history)}+ messages in history)"

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

        # Sub-agent 模式（compact_history=False）：精简 system prompt，跳过所有裁剪
        if not self._cfg.compact_history:
            from agent.prompt import build_sub_agent_system_prompt
            system_content = build_sub_agent_system_prompt(schemas)
            history_dicts = history.to_dicts()
            # Sub-agent 短生命周期，不做 trim —— ConversationHistory.max_messages 已兜底
            messages = [LLMMessage(role="system", content=system_content)]
            for d in history_dicts:
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

        # ── 以下为主 agent 的正常流程（compact_history=True）──────────
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

        # System prompt: 使用 StructuredContext 分层构建
        # - Layer 0 (SYSTEM, cacheable): 核心规则 + 工具定义 + repo summary
        # - Layer 1 (PROJECT, cacheable): auto_memory 使用指导（session 内稳定）
        # - Project context (memory/rules/skills) 在下面作为独立 user message 注入，不影响 system cache
        from context.structured import ContextLayer, ContextPriority, StructuredContext

        repo_path = getattr(self, "_current_repo_path", ".")
        core_text = build_system_prompt_core(repo_path, schemas, self._repo_map_cache)
        variable_text = build_system_prompt_variable(
            memory_section="",
            auto_memory_enabled=bool(self._memory_context and self._memory_context.enabled),
        )

        structured_ctx = StructuredContext()
        structured_ctx.add_layer(ContextLayer(
            name="system_core",
            priority=ContextPriority.SYSTEM,
            content=core_text,
            cacheable=True,
        ))
        if variable_text:
            structured_ctx.add_layer(ContextLayer(
                name="memory_guidance",
                priority=ContextPriority.PROJECT,
                content=variable_text,
                cacheable=True,
            ))

        enable_caching = self._is_anthropic_backend()
        system_content = structured_ctx.build_system_content(enable_caching=enable_caching)

        history_dicts = history.to_dicts()

        # Layer 2: Snip — 移除低价值轮次（零成本）
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
        构建项目上下文消息（当前任务 + 记忆索引 + 项目规则 + 可用 skills）。
        独立于 system prompt，变动不影响 prompt cache。
        每轮 _build_messages() 都会重建，保证即使 history 被 compacted 也不丢失任务锚点。
        """
        parts: list[str] = []

        # Task Anchor — 始终放在最前面，确保 LLM 每轮都能看到当前任务
        task_desc = getattr(self, "_current_task_description", "")
        if task_desc:
            parts.append(f"## Current Task\n{task_desc}")

        # Analysis 类型任务：注入行为覆盖，引导快速完成
        if getattr(self, "_task_intent", "edit") == "analysis":
            parts.append(
                "## Task Mode: Analysis\n"
                "This is a read-only analysis task. Your workflow is:\n"
                "1. Read the relevant code using targeted tools (file_read, file_view, search_text, find_symbol)\n"
                "2. Once you have sufficient information, respond directly with your answer\n"
                "Do NOT edit files. Do NOT run tests. Respond as soon as you can answer the question."
            )

        # 记忆索引
        if self._memory_context and self._memory_context.enabled:
            memory_section = self._memory_context.build_memory_section()
            if memory_section:
                parts.append(memory_section)

        # 项目规则文件
        rules_content = self._load_project_rules()
        if rules_content:
            parts.append(f"## Project Rules\n{rules_content}")

        # 可用 Skills（由 ChatSession 注入）
        skills_prompt = getattr(self, "_skills_prompt", "")
        if skills_prompt:
            parts.append(skills_prompt)

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
            parts.append(self._truncate_output(observation.output))
        if observation.error and not observation.is_success():
            parts.append(f"Error: {observation.error}")
        return "\n".join(parts) if parts else "(no output)"

    @staticmethod
    def _truncate_output(text: str, max_chars: int = 8000) -> str:
        """对超长 tool output 做预截断，保留首尾关键部分。"""
        if len(text) <= max_chars:
            return text
        keep = max_chars // 2
        head = text[:keep]
        tail = text[-keep:]
        omitted = len(text) - max_chars
        return f"{head}\n\n... [{omitted} chars omitted] ...\n\n{tail}"

    def _format_observations_for_history(self, observations: list[Observation]) -> str:
        """把多条 Observation 格式化为一条 user 消息（并行 tool_calls 用，text fallback mode）。"""
        lines = []
        for obs in observations:
            status = "SUCCESS" if obs.is_success() else "ERROR"
            lines.append(f"[Tool: {obs.tool_name} | {status}]")
            if obs.output:
                lines.append(self._truncate_output(obs.output))
            if obs.error and not obs.is_success():
                lines.append(f"Error: {obs.error}")
        return "\n".join(lines)

    def _is_looping(self, log: EventLog) -> bool:
        """
        检测是否陷入死循环。两级检测：

        1. 严格匹配：最近 N 条 action 的 (tool_name, params) 完全相同
        2. 语义匹配：最近 N+1 条 action 使用的 tool_name 多重集相同
           （反复调用相同类型的工具，只是参数略有不同——需要更多证据）
        """
        n = self._cfg.loop_detection_window
        actions = log.get_actions()
        if len(actions) < n:
            return False

        recent = actions[-n:]
        if not all(a.action_type == ActionType.TOOL_CALL for a in recent):
            return False
        if not all(a.tool_calls for a in recent):
            return False

        def _serialize_exact(action: Action) -> tuple:
            return tuple(
                (tc.name, tuple(sorted(tc.params.items())))
                for tc in action.tool_calls
            )

        def _serialize_names(action: Action) -> tuple:
            return tuple(sorted(tc.name for tc in action.tool_calls))

        # Level 1: exact match (window = N)
        first_exact = _serialize_exact(recent[0])
        if all(_serialize_exact(a) == first_exact for a in recent[1:]):
            return True

        # Level 2: semantic loop — same tool name multiset repeated N+1 times
        # Requires one more repetition than exact match to reduce false positives
        semantic_window = n + 1
        if len(actions) >= semantic_window:
            semantic_recent = actions[-semantic_window:]
            if all(a.action_type == ActionType.TOOL_CALL and a.tool_calls for a in semantic_recent):
                first_names = _serialize_names(semantic_recent[0])
                if all(_serialize_names(a) == first_names for a in semantic_recent[1:]):
                    return True

        return False

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
        if isinstance(self._full_registry, _SingleFileReadOnlyRegistry):
            return self._full_registry
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
        task_ctx = getattr(self, "_current_task_description", "")
        compacted = self.compactor.compact_history(history_dicts, task_context=task_ctx)
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

        Phase 1: 只读规划 → 生成 markdown 计划
        Phase 2: 用户审批后按任务意图执行
          - edit: 切换到完整工具权限实施修改
          - analysis: 继续只读执行并生成答案
        """
        from agent.plan import Plan
        from agent.prompt import get_plan_mode_injection, get_plan_execution_injection

        log.log_task_start(task)
        logger.info("PlanExecuteAgent starting task %s", task.task_id)

        constrained_path = _extract_single_file_constraint(task.description)
        active_registry = (
            _SingleFileReadOnlyRegistry(self._registry, constrained_path)
            if constrained_path and task.intent == "analysis"
            else self._registry
        )
        agent = ReActAgent(
            self._backend, active_registry, self._cfg,
            memory_context=self._memory_context,
        )
        history = ConversationHistory(
            max_messages=self._cfg.history_max_messages,
        )
        agent._pending_history = history

        total_plan_tokens = 0
        total_plan_steps = 0
        revision_feedback = ""
        max_plan_attempts = max(1, self._plan_cfg.max_replans + 1)
        plan_result: RunResult | None = None
        plan_text = ""

        for attempt in range(1, max_plan_attempts + 1):
            plan_result = self._run_planning_phase(
                agent, history, task, log, get_plan_mode_injection(), revision_feedback
            )
            total_plan_tokens += plan_result.total_tokens
            total_plan_steps += plan_result.steps_taken
            plan_text = plan_result.summary or ""
            if not plan_text.strip():
                return self._fallback_after_empty_plan(agent, task, log)

            plan = Plan.from_markdown(plan_text, task.description)
            log.log_plan_generated(plan)
            logger.info("Plan generated (%d chars): %s...", len(plan_text), plan_text[:100])

            approval = self._request_plan_approval(plan_text)
            if not approval.approved:
                reason = approval.feedback or "Plan rejected by user"
                log.log_task_failed(steps=total_plan_steps, reason=reason)
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.GAVE_UP,
                    summary=reason,
                    steps_taken=total_plan_steps,
                    total_tokens=total_plan_tokens,
                )
            if approval.action != "revise":
                break
            revision_feedback = approval.feedback or "Please revise the plan before execution."
            if attempt == max_plan_attempts:
                reason = f"Plan revision requested but max revisions reached: {revision_feedback}"
                log.log_task_failed(steps=total_plan_steps, reason=reason)
                return RunResult(
                    task_id=task.task_id,
                    status=RunStatus.GAVE_UP,
                    summary=reason,
                    steps_taken=total_plan_steps,
                    total_tokens=total_plan_tokens,
                )

        assert plan_result is not None

        exec_result = self._run_execution_phase(
            agent=agent,
            history=history,
            task=task,
            log=log,
            plan_text=plan_text,
            plan_result=plan_result,
            exec_injection=get_plan_execution_injection(),
            consumed_plan_steps=total_plan_steps,
            consumed_plan_tokens=total_plan_tokens,
        )

        if hasattr(agent, "_pending_history"):
            del agent._pending_history

        total_tokens = total_plan_tokens + exec_result.total_tokens
        total_steps = total_plan_steps + exec_result.steps_taken

        if exec_result.is_success():
            patch = _git_diff(task.repo_path) if task.intent == "edit" else None
            summary = exec_result.summary or "Plan executed successfully"
            log.log_task_complete(steps=total_steps, summary=summary)
            return RunResult(
                task_id=task.task_id,
                status=RunStatus.SUCCESS,
                summary=summary,
                steps_taken=total_steps,
                total_tokens=total_tokens,
                patch=patch,
                cache_stats=exec_result.cache_stats,
            )

        log.log_task_failed(steps=total_steps, reason=exec_result.summary)
        return RunResult(
            task_id=task.task_id,
            status=exec_result.status,
            summary=exec_result.summary,
            steps_taken=total_steps,
            total_tokens=total_tokens,
            error=exec_result.error,
            cache_stats=exec_result.cache_stats,
        )

    def _run_planning_phase(
        self,
        agent: ReActAgent,
        history: ConversationHistory,
        task: Task,
        log: EventLog,
        plan_injection: str,
        revision_feedback: str = "",
    ) -> RunResult:
        intent_label = "read-only answer" if task.intent == "analysis" else "implementation"
        revision_section = ""
        if revision_feedback:
            revision_section = f"\n\n## User Revision Feedback\n{revision_feedback}\nRevise the plan to address this feedback."
        constrained_path = _extract_single_file_constraint(task.description)
        constraint_section = ""
        if constrained_path and task.intent == "analysis":
            constraint_section = (
                f"\n\n## Enforced Tool Constraint\n"
                f"Only file_read/file_view on `{constrained_path}` are available. "
                "Do not use find_files/search_text/git/web/memory tools for this task."
            )
        history.add(LLMMessage(
            role="user",
            content=(
                f"{plan_injection}\n\n"
                f"## Repository\n{task.repo_path}\n\n"
                f"## Task Type\n{intent_label}\n\n"
                f"## Task\n{task.description}"
                f"{constraint_section}"
                f"{revision_section}\n\n"
                "Produce only an approval plan for the execution phase. "
                "Do not include the final answer or completed work in the plan. "
                "Call finish with the plan when ready."
            ),
        ))

        plan_steps = max(3, task.max_steps // 3)
        plan_task = Task(
            description=task.description,
            repo_path=task.repo_path,
            intent=task.intent,
            issue_url=task.issue_url,
            test_cmd=task.test_cmd,
            max_steps=plan_steps,
            budget_tokens=max(1, task.budget_tokens // 3),
        )

        agent.switch_to_plan_mode()
        plan_log = EventLog.create(plan_task, log_dir=self._plan_cfg.plan_subtask_log_dir)
        try:
            return agent.run(plan_task, plan_log)
        finally:
            plan_log.close()

    def _fallback_after_empty_plan(self, agent: ReActAgent, task: Task, log: EventLog) -> RunResult:
        logger.warning("Plan generation produced empty result — falling back")
        if task.intent == "analysis":
            agent.switch_to_plan_mode()
        else:
            agent.switch_to_execute_mode()
        fallback_task = Task(
            description=task.description,
            repo_path=task.repo_path,
            intent=task.intent,
            issue_url=task.issue_url,
            test_cmd=task.test_cmd,
            max_steps=task.max_steps,
            budget_tokens=task.budget_tokens,
        )
        return agent.run(fallback_task, log)

    def _request_plan_approval(self, plan_text: str):
        from agent.plan import PlanApproval

        approval_cb = self._plan_cfg.plan_approval_callback
        if not approval_cb:
            return PlanApproval(approved=True)

        raw = approval_cb(plan_text)
        if isinstance(raw, PlanApproval):
            return raw
        return PlanApproval(approved=bool(raw))

    def _run_execution_phase(
        self,
        agent: ReActAgent,
        history: ConversationHistory,
        task: Task,
        log: EventLog,
        plan_text: str,
        plan_result: RunResult,
        exec_injection: str,
        consumed_plan_steps: int | None = None,
        consumed_plan_tokens: int | None = None,
    ) -> RunResult:
        used_steps = plan_result.steps_taken if consumed_plan_steps is None else consumed_plan_steps
        used_tokens = plan_result.total_tokens if consumed_plan_tokens is None else consumed_plan_tokens
        exec_steps = max(3, task.max_steps - used_steps)
        exec_budget = max(1, task.budget_tokens - used_tokens)
        exec_task = Task(
            description=task.description,
            repo_path=task.repo_path,
            intent=task.intent,
            issue_url=task.issue_url,
            test_cmd=task.test_cmd,
            max_steps=exec_steps,
            budget_tokens=exec_budget,
        )

        constrained_path = _extract_single_file_constraint(task.description)
        mode_instruction = (
            "This is a read-only answer task. Keep read-only permissions active and obtain the answer now."
            if task.intent == "analysis"
            else "This is an implementation task. Use full execution permissions to implement the approved plan."
        )
        if constrained_path and task.intent == "analysis":
            mode_instruction += (
                f" Only file_read/file_view on `{constrained_path}` are available; "
                "do not use discovery/search tools or read any other path."
            )
        history.add(LLMMessage(
            role="user",
            content=(
                f"## Original Task\n{task.description}\n\n"
                f"## Approved Plan\n{plan_text}\n\n"
                f"{exec_injection}\n\n"
                f"## Execution Permission\n{mode_instruction}"
            ),
        ))

        if task.intent == "analysis":
            agent.switch_to_plan_mode()
        else:
            agent.switch_to_execute_mode()
        return agent.run(exec_task, log)

    def _get_git_diff(self, repo_path: str) -> str | None:
        return _git_diff(repo_path)