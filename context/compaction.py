"""
context/compaction.py

客户端对话压缩（Compaction）。

当对话历史接近 token 预算上限时，通过 LLM 调用将对话历史压缩为结构化摘要，
保留关键信息（发现了什么、修改了什么、还需要做什么），丢弃完整的工具输出。

使用场景：
- 自动：_build_messages() 检测到历史 token 超预算时触发
- 手动：用户输入 /compact 命令时触发

压缩策略：
- 优先使用 LLM 生成高质量摘要（保留语义、因果关系）
- LLM 不可用时回退到 regex 提取（兼容无 backend 场景）
"""

from __future__ import annotations

import re
import logging
from typing import Any, TYPE_CHECKING

from context.token_budget import estimate_tokens

if TYPE_CHECKING:
    from llm.base import LLMBackend

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_COMPACTION_TRIGGER_RATIO = 0.80  # 历史 token 超过预算的 80% 时触发
_COMPACTION_BLOCK_TOKEN_BUDGET = 2000  # compaction 块的目标 token 数
_MIN_HISTORY_BEFORE_COMPACT = 6  # 少于 N 条消息时不做 compaction
_MAX_CONSECUTIVE_COMPACTIONS = 3  # 连续 compaction 次数上限（thrashing 保护）
_COMPACTION_COOLDOWN_STEPS = 3  # compaction 后冷却步数，期间不再触发

_SUMMARIZE_SYSTEM_PROMPT = """\
You are a conversation summarizer. Condense the given conversation into a \
structured summary that preserves all essential information for continuing the work.

Focus on:
1. What was discovered (files found, code structure, bugs identified)
2. What was changed (edits made, commands run, their outcomes)
3. What remains to do (unresolved issues, next steps)

Output format — use this exact structure:
## Discoveries
- <key findings, one per line>

## Changes Made
- <each edit/action and its result>

## Remaining Work
- <what still needs to be done>

Be concise but complete. Preserve file paths, function names, and error messages exactly.\
"""

# 正则：匹配 forge-agent 的纯文本格式
# assistant: "Thought: ...\nAction: tool_name\nParams: {...}"
# user: "[Tool: tool_name | SUCCESS]\noutput..."
_RE_THOUGHT = re.compile(r"Thought:\s*(.+?)(?:\n|$)", re.DOTALL)
_RE_ACTION = re.compile(r"Action:\s*(\S+)")
_RE_PARAMS = re.compile(r"Params:\s*(\{.*\})", re.DOTALL)
_RE_OBSERVATION = re.compile(
    r"\[Tool:\s*(\S+)\s*\|\s*(\w+)\]\s*\n?(.*)",
    re.DOTALL,
)
_RE_TRUNCATED = re.compile(r"\[(\d+) earlier messages were truncated")


# ---------------------------------------------------------------------------
# ConversationCompactor
# ---------------------------------------------------------------------------

class ConversationCompactor:
    """
    对话历史压缩器。

    优先通过 LLM 生成高质量摘要，LLM 不可用时回退到 regex 提取。
    内置 thrashing 保护：连续触发超过阈值时停止自动 compaction。
    """

    def __init__(
        self,
        trigger_ratio: float = _COMPACTION_TRIGGER_RATIO,
        compact_budget: int = _COMPACTION_BLOCK_TOKEN_BUDGET,
        min_history: int = _MIN_HISTORY_BEFORE_COMPACT,
        backend: "LLMBackend | None" = None,
        max_consecutive: int = _MAX_CONSECUTIVE_COMPACTIONS,
        cooldown_steps: int = _COMPACTION_COOLDOWN_STEPS,
    ) -> None:
        self._trigger_ratio = trigger_ratio
        self._compact_budget = compact_budget
        self._min_history = min_history
        self._backend = backend
        self._max_consecutive = max_consecutive
        self._cooldown_steps = cooldown_steps
        self._consecutive_compactions = 0
        self._steps_since_last_compact = cooldown_steps  # 初始允许触发

    # ------------------------------------------------------------------
    # 判断是否需要 compaction
    # ------------------------------------------------------------------

    @property
    def is_thrashing(self) -> bool:
        """连续 compaction 次数是否超过阈值。"""
        return self._consecutive_compactions >= self._max_consecutive

    def reset_thrashing_counter(self) -> None:
        """重置 thrashing 计数器（用户主动输入后调用）。"""
        self._consecutive_compactions = 0

    def tick_step(self) -> None:
        """每步调用一次，推进冷却计数器。"""
        self._steps_since_last_compact += 1

    def should_compact(
        self,
        history_dicts: list[dict],
        history_budget: int,
    ) -> bool:
        """
        判断是否需要 compaction。

        内置 thrashing 保护：连续触发超过阈值时返回 False。

        Args:
            history_dicts: history.to_dicts() 的输出
            history_budget: 本轮历史配额（plan.history），触发阈值 = budget × trigger_ratio

        Returns:
            True 表示需要 compaction
        """
        if len(history_dicts) < self._min_history:
            return False

        if self.is_thrashing:
            logger.warning(
                "Compaction thrashing detected (%d consecutive). "
                "Skipping auto-compaction — user interaction will reset.",
                self._consecutive_compactions,
            )
            return False

        if self._steps_since_last_compact < self._cooldown_steps:
            return False

        total_tokens = sum(
            estimate_tokens(m.get("content", "")) for m in history_dicts
        )
        threshold = int(history_budget * self._trigger_ratio)
        return total_tokens > threshold

    # ------------------------------------------------------------------
    # 执行 compaction
    # ------------------------------------------------------------------

    def compact_history(
        self,
        history_dicts: list[dict],
        max_tokens: int | None = None,
        task_context: str = "",
    ) -> list[dict]:
        """
        压缩对话历史（渐进式）。

        如果历史中已有一个 compact block（之前压缩过的），
        只对该 block 之后的新消息做增量压缩并追加到已有摘要中，
        避免重复压缩已有摘要。

        Args:
            history_dicts: history.to_dicts() 的输出
            max_tokens:   compaction 块的目标 token 数
            task_context: 当前任务描述，用于引导摘要优先保留任务相关信息

        Returns:
            压缩后的历史 dict 列表：[保留的首条, compact 块, 最后几轮原始]
        """
        self._task_context = task_context
        if not history_dicts:
            return history_dicts

        self._consecutive_compactions += 1
        self._steps_since_last_compact = 0

        budget = max_tokens or self._compact_budget
        first = history_dicts[0]  # 保留首条任务描述
        rest = history_dicts[1:]  # 其余消息

        if not rest:
            return [first]

        # 检测已有的 compact block（渐进式压缩）
        existing_compact_idx = self._find_existing_compact_block(rest)

        # 1. 从 rest 中提取最后几轮（保留最近 2-3 轮原始消息）
        keep_recent = self._extract_recent_rounds(rest, n_rounds=2)

        # 2. 确定需要新压缩的消息范围
        compact_end = max(0, len(rest) - len(keep_recent))

        if existing_compact_idx is not None:
            # 渐进式：保留已有 compact block，只压缩它之后的新消息
            existing_block = rest[existing_compact_idx]
            new_messages_start = existing_compact_idx + 1
            new_targets = rest[new_messages_start:compact_end]

            if new_targets:
                # 增量摘要：只压缩新消息
                incremental_summary = self._summarize_messages(new_targets, budget // 2)
                # 合并到已有摘要
                merged_block = self._merge_compact_blocks(
                    existing_block, incremental_summary, len(new_targets)
                )
                result = [first, merged_block] + keep_recent
            else:
                result = [first, existing_block] + keep_recent
        else:
            # 首次压缩：全量压缩
            compact_targets = rest[:compact_end]
            if compact_targets:
                compact_block = self._build_compact_block(compact_targets, budget)
                result = [first, compact_block] + keep_recent
            else:
                result = [first] + keep_recent

        return result

    def _find_existing_compact_block(self, messages: list[dict]) -> int | None:
        """查找已有的 compact block 在消息列表中的索引。"""
        for i, msg in enumerate(messages):
            content = msg.get("content", "")
            if content.startswith("[Earlier conversation summarized") or \
               content.startswith("[Conversation compacted"):
                return i
        return None

    def _merge_compact_blocks(
        self, existing_block: dict, new_summary: str, new_msg_count: int
    ) -> dict:
        """将增量摘要合并到已有的 compact block。"""
        existing_content = existing_block.get("content", "")

        # 找到已有摘要的结尾标记位置
        end_marker = "[Continue below with the most recent exchanges.]"
        if end_marker in existing_content:
            base = existing_content[:existing_content.index(end_marker)].rstrip()
        else:
            base = existing_content.rstrip()

        merged_content = (
            f"{base}\n\n"
            f"--- Incremental update (+{new_msg_count} messages) ---\n"
            f"{new_summary}\n\n"
            f"{end_marker}"
        )

        return {"role": "user", "content": merged_content}

    def build_compact_block_for_history(
        self,
        history_dicts: list[dict],
        max_tokens: int | None = None,
    ) -> dict:
        """
        为完整历史生成一段 compaction 块（/compact 命令用）。

        保留首条，压缩剩余全部。

        Returns:
            {"role": "user", "content": compact_text}
        """
        if not history_dicts:
            return {"role": "user", "content": "(empty conversation)"}

        budget = max_tokens or self._compact_budget
        first = history_dicts[0]
        rest = history_dicts[1:]

        compact_text = self._summarize_messages(rest, budget)

        return {
            "role": "user",
            "content": f"[Conversation compacted — earlier messages summarized]\n\n"
                       f"Original task: {first.get('content', '')[:200]}\n\n"
                       f"{compact_text}\n\n"
                       f"[End of compaction summary. Resume conversation.]",
        }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _extract_recent_rounds(
        self,
        messages: list[dict],
        n_rounds: int = 2,
    ) -> list[dict]:
        """
        从消息列表中提取最近 N 轮（assistant + response 对）。
        从后往前找，保留最后的 N 个配对。

        支持两种格式：
        - Text mode: assistant → user (tool result as user message)
        - Native mode: assistant (tool_calls) → tool (tool_call_id)
        """
        if not messages:
            return []

        # 从后往前遍历，找 assistant + (user|tool) 对
        rounds: list[list[dict]] = []
        current_round: list[dict] = []

        for msg in reversed(messages):
            role = msg.get("role", "")
            current_round.insert(0, msg)

            if role == "assistant" and current_round:
                rounds.insert(0, current_round)
                current_round = []
                if len(rounds) >= n_rounds:
                    break
            elif role == "tool":
                # Native tool result — belongs to the current round (with its assistant)
                pass
            elif role == "user":
                if msg.get("tool_call_id"):
                    # Legacy: tool result encoded as user message with tool_call_id
                    pass
                elif current_round and current_round[0].get("role") == "user":
                    # Standalone user message starts a new round boundary
                    rounds.insert(0, current_round)
                    current_round = [msg]
                    if len(rounds) >= n_rounds:
                        break
                else:
                    current_round = [msg]

        # 把选中的轮次展开回列表
        selected = []
        for rnd in rounds:
            selected.extend(rnd)

        # 确定有多少条消息被保留了，返回对应的原始消息
        count = len(selected)
        return messages[-count:] if count > 0 else []

    def _build_compact_block(
        self,
        messages: list[dict],
        max_tokens: int,
    ) -> dict:
        """把一批消息压缩成一段 compact 块。"""
        text = self._summarize_messages(messages, max_tokens)

        return {
            "role": "user",
            "content": (
                f"[Earlier conversation summarized — {len(messages)} messages "
                f"compacted]\n\n{text}\n\n"
                f"[Continue below with the most recent exchanges.]"
            ),
        }

    def _summarize_messages(
        self,
        messages: list[dict],
        max_tokens: int,
    ) -> str:
        """
        把消息列表压缩成紧凑摘要。

        优先使用 LLM 生成高质量摘要（保留语义和因果关系）；
        LLM 不可用时回退到 regex 提取。
        """
        if self._backend:
            llm_summary = self._summarize_with_llm(messages, max_tokens)
            if llm_summary:
                return llm_summary

        return self._summarize_with_regex(messages, max_tokens)

    def _summarize_with_llm(
        self,
        messages: list[dict],
        max_tokens: int,
    ) -> str | None:
        """通过 LLM 调用生成对话摘要。失败时返回 None。"""
        from llm.base import LLMMessage as Msg

        conversation_text = self._format_messages_for_llm(messages)
        if not conversation_text.strip():
            return None

        # 如果有当前任务上下文，注入到摘要 prompt 中引导优先保留相关信息
        task_hint = ""
        task_ctx = getattr(self, "_task_context", "")
        if task_ctx:
            task_hint = (
                f"\n\nIMPORTANT: The user's current task is: \"{task_ctx}\"\n"
                f"Prioritize preserving information relevant to completing this task."
            )

        user_prompt = (
            f"Summarize the following conversation. "
            f"Keep the summary under {max_tokens} tokens.{task_hint}\n\n"
            f"--- CONVERSATION ---\n{conversation_text}\n--- END ---"
        )

        try:
            response = self._backend.complete(
                messages=[
                    Msg(role="system", content=_SUMMARIZE_SYSTEM_PROMPT),
                    Msg(role="user", content=user_prompt),
                ],
                tools=[],
            )
            summary = response.raw_content.strip()
            if summary and estimate_tokens(summary) <= max_tokens * 1.5:
                logger.info("LLM-based compaction produced %d token summary", estimate_tokens(summary))
                return summary
            if summary:
                chars = max_tokens * 4
                return summary[:chars]
        except Exception as exc:
            logger.warning("LLM-based compaction failed, falling back to regex: %s", exc)

        return None

    def _format_messages_for_llm(self, messages: list[dict]) -> str:
        """将消息列表格式化为 LLM 可读的对话文本。"""
        import json as _json

        parts: list[str] = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "") or ""

            # 跳过 compaction 块自身
            if isinstance(content, str) and (
                content.startswith("[Earlier conversation summarized")
                or content.startswith("[Conversation compacted")
            ):
                continue

            # Native tool_calls 模式
            if msg.get("tool_calls"):
                tc_parts = [content] if content else []
                for tc in msg["tool_calls"]:
                    tc_parts.append(
                        f"[Called: {tc['name']}({_json.dumps(tc.get('params', {}), ensure_ascii=False)[:200]})]"
                    )
                content = "\n".join(tc_parts)
            elif role == "tool":
                # Native tool result
                tool_id = msg.get("tool_call_id", "?")
                content = f"[Tool result ({tool_id})]: {content}"

            if not isinstance(content, str):
                content = str(content)

            # 截断过长的单条消息（工具输出等）
            if len(content) > 2000:
                content = content[:1800] + "\n... (truncated)"
            parts.append(f"[{role}]\n{content}")
        return "\n\n".join(parts)

    def _summarize_with_regex(
        self,
        messages: list[dict],
        max_tokens: int,
    ) -> str:
        """回退方案：通过 regex 提取关键信息。"""
        entries: list[str] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "") or ""

            if role == "assistant":
                extracted = self._extract_from_assistant(
                    content, tool_calls=msg.get("tool_calls")
                )
                if extracted:
                    entries.append(extracted)

            elif role == "tool":
                # Native tool result
                extracted = self._extract_native_tool_result(content)
                if extracted:
                    entries.append(extracted)

            elif role == "user":
                extracted = self._extract_from_observation(content)
                if extracted:
                    entries.append(extracted)

        summary = "\n".join(entries)

        if estimate_tokens(summary) > max_tokens:
            chars = max_tokens * 4
            summary = summary[:chars]
            summary += f"\n... (truncated to fit budget)"

        return summary

    def _extract_from_assistant(
        self, content: str, tool_calls: list[dict] | None = None
    ) -> str | None:
        """从 assistant 消息提取 thought + tool call 摘要。"""
        if not isinstance(content, str):
            content = str(content) if content else ""

        # Native tool_calls 模式：直接从结构化数据提取
        if tool_calls:
            parts = []
            if content and content.strip():
                parts.append(f"→ {content.strip()[:200]}")
            for tc in tool_calls:
                param_info = self._extract_key_params(tc.get("params", {}))
                parts.append(f"  🛠 {tc['name']}{param_info}")
            return "\n".join(parts) if parts else None

        # Text fallback 模式：regex 提取
        if not content.strip():
            return None

        # 检查 compaction 块自身（避免递归）
        if content.startswith("[Earlier conversation summarized") or \
           content.startswith("[Conversation compacted"):
            return None

        # 提取 thought（第一行，或 Thought: 后的内容）
        thought_match = _RE_THOUGHT.search(content)
        action_match = _RE_ACTION.search(content)
        params_match = _RE_PARAMS.search(content)

        parts = []

        if thought_match:
            thought = thought_match.group(1).strip()[:200]
            if thought:
                parts.append(f"→ {thought}")

        if action_match:
            tool_name = action_match.group(1)
            param_info = ""
            if params_match:
                try:
                    import json
                    params = json.loads(params_match.group(1))
                    param_info = self._extract_key_params(params)
                except (json.JSONDecodeError, ValueError):
                    pass
            parts.append(f"  🛠 {tool_name}{param_info}")

        return "\n".join(parts) if parts else None

    def _extract_key_params(self, params: dict) -> str:
        """提取工具调用中的关键参数作为摘要。"""
        key_params = []
        for k in ("cmd", "path", "file_path", "pattern", "name"):
            if k in params:
                key_params.append(f"{k}={params[k]}")
        if key_params:
            return " (" + ", ".join(key_params) + ")"
        return ""

    def _extract_native_tool_result(self, content: str) -> str | None:
        """从 native tool result 消息提取摘要。"""
        if not content or not content.strip():
            return None
        key_info = self._extract_key_output(content)
        result = f"  ✓ [tool result]"
        if key_info:
            result += f": {key_info}"
        return result

    def _extract_from_observation(self, content: str) -> str | None:
        """从 user/observation 消息提取工具结果摘要。"""
        if not content.strip():
            return None

        # 跳过 compaction 块自身
        if content.startswith("[Earlier conversation summarized") or \
           content.startswith("[Conversation compacted"):
            return None

        # 匹配 [Tool: name | STATUS]
        obs_match = _RE_OBSERVATION.match(content.strip())
        if not obs_match:
            return None

        tool_name = obs_match.group(1)
        status = obs_match.group(2)
        output = obs_match.group(3).strip()

        # 提取输出的关键信息
        key_info = self._extract_key_output(output)
        status_icon = "✓" if status == "SUCCESS" else "✗"

        result = f"  {status_icon} [{tool_name}]"
        if key_info:
            result += f": {key_info}"
        return result

    def _extract_key_output(self, output: str) -> str:
        """
        从工具输出中提取关键信息。

        策略：
        1. 取第一行有实质内容的（跳过空行/分隔符）
        2. 如果包含关键信息（test results, error, file paths），保留
        3. 截断到 150 字符
        """
        if not output:
            return ""

        lines = output.splitlines()
        meaningful = []

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            # 跳过装饰性分隔符行
            if all(c in "─━═-*_— " for c in stripped):
                continue
            # 跳过纯数字行
            if stripped.isdigit():
                continue

            meaningful.append(stripped)

        if not meaningful:
            return ""

        # 取前 N 行
        preview = meaningful[:3]
        text = "; ".join(preview)

        if len(meaningful) > 3:
            text += f" ... ({len(meaningful) - 3} more lines)"

        # 截断到 150 字
        if len(text) > 150:
            text = text[:147] + "..."

        return text


# ---------------------------------------------------------------------------
# 摘要持久化
# ---------------------------------------------------------------------------

_SESSION_SUMMARY_FILENAME = "session_summary.md"


def persist_compaction_summary(summary_text: str, store_dir: str) -> None:
    """
    将 compaction 摘要持久化到磁盘。

    文件路径：~/.forge-agent/projects/<hash>/session_summary.md
    下次 session 启动时可以读取此文件恢复上下文。
    """
    from pathlib import Path

    path = Path(store_dir) / _SESSION_SUMMARY_FILENAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# Session Summary\n\n"
            f"Last updated by auto-compaction.\n\n"
            f"{summary_text}\n",
            encoding="utf-8",
        )
        logger.info("Compaction summary persisted to %s", path)
    except OSError as exc:
        logger.warning("Failed to persist compaction summary: %s", exc)


def load_session_summary(store_dir: str) -> str:
    """
    从磁盘加载上次 session 的 compaction 摘要。

    Returns:
        摘要文本，不存在时返回空字符串。
    """
    from pathlib import Path

    path = Path(store_dir) / _SESSION_SUMMARY_FILENAME
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8").strip()
        # 跳过只有标题行的情况
        if text.count("\n") <= 2:
            return ""
        return text
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------

def create_compactor(
    trigger_ratio: float = _COMPACTION_TRIGGER_RATIO,
    backend: "LLMBackend | None" = None,
) -> ConversationCompactor:
    """创建 ConversationCompactor，可选传入 LLM backend 以启用智能摘要。"""
    return ConversationCompactor(trigger_ratio=trigger_ratio, backend=backend)


# ---------------------------------------------------------------------------
# Layer 2: Snip — 低价值轮次过滤（零成本）
# ---------------------------------------------------------------------------

def snip_low_value_turns(history_dicts: list[dict]) -> list[dict]:
    """
    移除低价值的轮次，节省上下文空间。

    丢弃规则：
    - tool result 为空的 tool_use（如 grep 没找到、list 为空）
    - 被用户拒绝的 tool call（error 含 "rejected"）
    - observation 状态为 error 且 output 为空

    返回新的消息列表，不修改原列表。
    """
    if not history_dicts:
        return history_dicts

    # 标记哪些 assistant 消息应该被保留
    # 思路：从后往前遍历，如果 user 消息和对应的 assistant 消息都符合丢弃条件
    # 则两者都丢弃
    keep = [True] * len(history_dicts)

    # 标记 tool result content 为空或仅为 "[]" / "{}" / "" 的 user 消息
    for i, msg in enumerate(history_dicts):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "").strip()
        # 空结果：没有输出的 observation
        if not content or content in ("[]", "{}", "()", "None", "null"):
            keep[i] = False
            # 也丢弃前一条 assistant 消息（如果存在）
            if i > 0 and history_dicts[i - 1].get("role") == "assistant":
                keep[i - 1] = False
            continue
        # 被拒绝的 tool call
        if "rejected" in content.lower() or "blocked" in content.lower():
            keep[i] = False
            if i > 0 and history_dicts[i - 1].get("role") == "assistant":
                keep[i - 1] = False
            continue
        # 纯错误信息且无输出
        if content.startswith("[Tool:") and "ERROR" in content and "Error:" in content and "\n" not in content.split("Error:", 1)[0].strip():
            keep[i] = False
            if i > 0 and history_dicts[i - 1].get("role") == "assistant":
                keep[i - 1] = False

    # 保留首条（任务描述）
    keep[0] = True

    return [msg for i, msg in enumerate(history_dicts) if keep[i]]


# ---------------------------------------------------------------------------
# Layer 3: 滑动窗口裁剪（零成本）
# ---------------------------------------------------------------------------

def trim_sliding_window(
    history_dicts: list[dict],
    token_limit: int,
    keep_recent: int = 3,
) -> list[dict]:
    """
    滑动窗口裁剪：保留最近 N 轮完整，旧轮逐步降级。

    策略（从新到旧）：
    1. 最后 keep_recent 轮 (assistant + user) — 完整保留（在 prompt cache 中）
    2. 之前的轮次 — 丢弃 tool_result，只保留 assistant 的 thought
    3. 如果还不够 — 丢弃 assistant 的 Action/Params，只保留 thought
    4. 首条（任务描述）— 永远保留

    Args:
        history_dicts: history.to_dicts() 的输出
        token_limit:   历史配额的 token 上限
        keep_recent:   保留的最近完整轮次数

    Returns:
        裁剪后的消息列表
    """
    if not history_dicts or len(history_dicts) < 3:
        return history_dicts

    first = history_dicts[0]
    rest = history_dicts[1:]

    # 计算 token 数
    token_counts = [estimate_tokens(m.get("content", "")) for m in rest]
    total_rest = sum(token_counts)

    if total_rest <= token_limit - estimate_tokens(first.get("content", "")):
        return history_dicts  # 不需要裁

    # 从后往前分轮次（assistant + user 为一轮）
    rounds: list[list[dict]] = []
    current_round: list[dict] = []

    for msg in reversed(rest):
        current_round.insert(0, msg)
        if msg.get("role") == "assistant" and current_round:
            rounds.insert(0, current_round)
            current_round = []
        elif msg.get("role") == "user" and current_round and current_round[0].get("role") == "user":
            # 连续 user 消息，新的一轮从这个 user 开始
            # （上一个 user 已在上一轮中）
            pass

    # 如果最后一轮不完整（只有 user 消息），补进去
    if current_round:
        if rounds:
            rounds[-1].extend(current_round)
        else:
            rounds.append(current_round)

    if not rounds:
        return [first]

    # 保留最近 keep_recent 轮完整
    recent_rounds = rounds[-keep_recent:] if len(rounds) > keep_recent else rounds
    old_rounds = rounds[:-keep_recent] if len(rounds) > keep_recent else []

    if not old_rounds:
        # 历史轮次数量不够，全部保留
        result = [first]
        for r in recent_rounds:
            result.extend(r)
        return result

    # 对旧轮次逐级压缩
    compressed_old: list[dict] = []
    for rnd in old_rounds:
        compressed = _compress_round(rnd)
        compressed_old.extend(compressed)

    # 组装最终结果
    result = [first]
    if compressed_old:
        # 检查 token 预算
        old_tokens = sum(estimate_tokens(m.get("content", "")) for m in compressed_old)
        recent_tokens = sum(estimate_tokens(m.get("content", "")) for m in sum(recent_rounds, []))
        first_tokens = estimate_tokens(first.get("content", ""))
        total = first_tokens + old_tokens + recent_tokens

        if total <= token_limit:
            result.extend(compressed_old)
            result.extend(sum(recent_rounds, []))
            return result

        # 还不够：进一步压缩，对旧轮次只保留 thought
        compressed_old_thoughts = []
        for msg in compressed_old:
            if msg.get("role") == "assistant":
                thought = _extract_thought_only(msg.get("content", ""))
                if thought:
                    compressed_old_thoughts.append({"role": "assistant", "content": thought})
                # 丢弃 thought 也为空的消息
            # user 消息（tool result）直接丢弃

        old_tokens_2 = sum(estimate_tokens(m.get("content", "")) for m in compressed_old_thoughts)
        total_2 = first_tokens + old_tokens_2 + recent_tokens

        if total_2 <= token_limit or not compressed_old_thoughts:
            result.extend(compressed_old_thoughts)
            result.extend(sum(recent_rounds, []))
            return result

    # 兜底：保留首条 + 最近 keep_recent 轮
    result = [first]
    # 加一个占位符
    placeholder = {
        "role": "user",
        "content": f"[{len(old_rounds)} earlier rounds were compressed to fit context window]",
    }
    result.append(placeholder)
    for r in recent_rounds:
        result.extend(r)
    return result


def _compress_round(round_msgs: list[dict]) -> list[dict]:
    """
    压缩一轮消息：丢弃 user 的 tool_result，但保留 assistant 的 thought。
    """
    result = []
    for msg in round_msgs:
        if msg.get("role") == "assistant":
            # 保留 thought，丢弃 Action/Params 只占位置的细节
            thought = _extract_thought_only(msg.get("content", ""))
            if thought:
                result.append({"role": "assistant", "content": thought})
        # user 消息（tool result）丢弃
    return result


def _extract_thought_only(content: str) -> str | None:
    """从 assistant 消息中提取 thought 部分，去掉 Action/Params。"""
    idx = content.find("Action:")
    if idx == -1:
        return content.strip() or None
    thought = content[:idx].strip()
    if thought and not thought.startswith("[Earlier"):
        return thought
    return None
