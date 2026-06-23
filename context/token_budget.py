"""
context/token_budget.py

Token 预算管理：给 prompt 各部分分配 token 配额，超出时按优先级裁剪。

算法：配合模型实际 max_context_window，按已消耗 token 量衰减历史配额。

各部分优先级（高→低，裁剪时从低优先级开始）：
  1. system_core   系统指令，永不裁剪
  2. task          任务描述，永不裁剪
  3. repo_map      repo 摘要，超出时缩减
  4. recent_obs    最近 observation，永不裁剪
  5. history       历史对话，从最旧开始裁剪
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Token 计数：优先 tiktoken，失败时字符估算 fallback
# ---------------------------------------------------------------------------

_tiktoken_enc = None
_tiktoken_available = False
_init_lock = threading.Lock()

def _init_tiktoken() -> None:
    global _tiktoken_enc, _tiktoken_available
    if _tiktoken_available or _tiktoken_enc is not None:
        return
    with _init_lock:
        if _tiktoken_available or _tiktoken_enc is not None:
            return
        try:
            import tiktoken
            _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
            _tiktoken_available = True
        except Exception:
            _tiktoken_available = False


def estimate_tokens(text: str) -> int:
    """
    估算文本的 token 数。
    优先使用 tiktoken（精确），不可用时用字符数 // 4（误差 <15%）。
    """
    if not _tiktoken_available:
        _init_tiktoken()

    if _tiktoken_available and _tiktoken_enc is not None:
        try:
            return max(1, len(_tiktoken_enc.encode(text)))
        except Exception:
            pass

    # 字符估算 fallback
    return max(1, len(text) // 4)


def _estimate_msg_tokens(msg: dict) -> int:
    """
    估算单条消息 dict 的 token 数，兼容 native tool_use 和 text 两种格式。
    """
    import json as _json

    content = msg.get("content", "")
    if isinstance(content, list):
        tokens = sum(estimate_tokens(_json.dumps(block)) for block in content)
    elif isinstance(content, str):
        tokens = estimate_tokens(content)
    else:
        tokens = estimate_tokens(str(content))

    # tool_calls 字段贡献额外 tokens
    if msg.get("tool_calls"):
        for tc in msg["tool_calls"]:
            tokens += estimate_tokens(_json.dumps(tc))

    return tokens


def _get_content_str(msg: dict) -> str:
    """从消息 dict 中取 content 为 str（native 模式下 content 可能为 None）。"""
    content = msg.get("content", "")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return str(content)


def estimate_chars(tokens: int) -> int:
    """把 token 数转换为字符预算（估算）。"""
    return tokens * 4


def is_tiktoken_available() -> bool:
    """返回 tiktoken 是否可用，供诊断脚本使用。"""
    _init_tiktoken()
    return _tiktoken_available


# ---------------------------------------------------------------------------
# BudgetPlan
# ---------------------------------------------------------------------------

@dataclass
class BudgetPlan:
    """各部分的 token 配额计划。"""
    total: int
    system_core: int
    repo_map: int
    history: int
    observation: int
    reserve: int                     # 保留向后兼容，新代码为 0

    @property
    def available(self) -> int:
        return self.total - self.reserve


# ---------------------------------------------------------------------------
# TokenBudget
# ---------------------------------------------------------------------------

# 给 model response 预留的 token 空间（代替旧的 15% reserve）
_OUTPUT_ROOM = 4096

# 衰减公式分母系数：consumed 达到 effective × DENOM 时 decay=0.67
_DECAY_DENOM = 3

# 衰减下限，防止历史配额收缩到不可用
_DECAY_FLOOR = 0.30


class TokenBudget:
    """
    Token 预算管理器——消费感知的动态分配。

    用法：
        budget = TokenBudget(total=80_000)
        plan = budget.compute_plan(consumed_tokens=0, max_context_window=200_000)
        trimmed = budget.trim_to(text, plan.repo_map)
        trimmed_history = budget.trim_history(msgs, plan.history)
    """

    def __init__(self, total: int = 80_000) -> None:
        self._total = total

    def compute_plan(
        self,
        consumed_tokens: int = 0,
        max_context_window: int | None = None,
    ) -> BudgetPlan:
        """
        根据已消耗 token 数和模型上下文窗口，计算本轮配额。

        Args:
            consumed_tokens:  本轮之前已消耗的 token 总数（input + output）
            max_context_window: 模型的最大上下文窗口，None 时用 self._total

        Returns:
            BudgetPlan，其中 history 随 consumed_tokens 增大而衰减
        """
        effective = self._total
        if max_context_window is not None:
            effective = min(self._total, max_context_window)

        available = effective - _OUTPUT_ROOM

        if available <= 0:
            # 极端情况：上下文窗口太小，给保底配额
            return BudgetPlan(
                total=effective,
                reserve=0,
                system_core=max(500, effective // 4),
                repo_map=0,
                history=max(500, effective // 4),
                observation=0,
            )

        # 衰减系数：consumed_tokens 越大，历史配给越紧
        decay = _DECAY_FLOOR
        if consumed_tokens == 0:
            decay = 1.0
        else:
            decay = max(
                _DECAY_FLOOR,
                1.0 - consumed_tokens / (effective * _DECAY_DENOM),
            )

        # 固定开销组件（上限保护）
        system_core = max(2000, int(available * 0.12))
        repo_map = min(int(available * 0.12), 12_000)
        observation = max(1000, int(available * 0.10))

        # 历史 = 剩余空间 × 衰减
        base_history = available - system_core - repo_map - observation
        if base_history <= 0:
            base_history = available // 2

        if consumed_tokens == 0:
            history = base_history
        else:
            history = max(2000, int(base_history * decay))

        return BudgetPlan(
            total=effective,
            reserve=0,
            system_core=system_core,
            repo_map=repo_map,
            history=history,
            observation=observation,
        )

    def default_plan(self) -> BudgetPlan:
        """
        向后兼容的默认计划，等价于 compute_plan(0)。
        """
        return self.compute_plan(consumed_tokens=0, max_context_window=None)

    def trim_to(self, text: str, token_limit: int) -> str:
        """裁剪文本到 token_limit 以内，超出时保留开头。"""
        if estimate_tokens(text) <= token_limit:
            return text
        # 二分逼近：找到合适的字符截断点
        char_limit = token_limit * 4
        candidate = text[:char_limit]
        while estimate_tokens(candidate) > token_limit and len(candidate) > 0:
            candidate = candidate[:int(len(candidate) * 0.9)]
        omitted = estimate_tokens(text[len(candidate):])
        return candidate + f"\n... [{omitted} tokens truncated]"

    def trim_history(
        self,
        messages: list[dict],
        token_limit: int,
    ) -> list[dict]:
        """
        裁剪历史消息列表到 token_limit 以内。
        保留第一条（任务描述）+ 尽量多的最近消息。

        分级策略（从轻到重，按消息重要性差异化裁剪）：
        0. 按优先级排序，低优先级消息优先丢弃
        1. 保留 tool_use，丢弃 tool_result（旧工具输出）
        2. 丢弃旧 tool_use 记录，保留 thought
        3. 仅保留最后 N 轮
        """
        if not messages:
            return messages

        token_counts = [_estimate_msg_tokens(m) for m in messages]
        total = sum(token_counts)

        if total <= token_limit:
            return messages

        # ── 第 0 级：按优先级裁剪低重要性消息 ─────────────────────
        result = self._trim_by_priority(messages, token_counts, token_limit)
        if result is not None:
            return result

        # ── 第 1 级：尝试丢弃旧 observation（tool result） ────────────
        result = self._trim_results_only(messages, token_counts, token_limit)
        if result is not None:
            return result

        # ── 第 2 级：尝试丢弃旧 tool_use，保留推理 ───────────────────
        result = self._trim_tool_calls(messages, token_counts, token_limit)
        if result is not None:
            return result

        # ── 第 3 级：回退到原始简单策略 ────────────────────────────
        return self._trim_simple(messages, token_counts, token_limit)

    @staticmethod
    def _message_priority(msg: dict, index: int, total: int) -> int:
        """
        计算消息的重要性优先级（越高越重要）。

        优先级分级：
        - 5: 首条（任务描述）、用户原始输入
        - 4: Reflection 提示、compact block
        - 3: assistant 推理（含 Thought）
        - 2: 工具调用（assistant + Action: 或 native tool_calls）
        - 1: 工具输出（user + [Tool: 或 role="tool"）
        - 0: 空消息或截断占位符

        近期消息额外加分（最近 1/4 的消息 +2）。
        """
        content = _get_content_str(msg)
        role = msg.get("role", "")

        # 首条始终最高优先级
        if index == 0:
            return 10

        # 基础优先级
        if role == "tool":
            # Native tool result
            priority = 1
        elif role == "user":
            if content.startswith("[Tool:"):
                priority = 1  # 工具输出 (text fallback)
            elif content.startswith("[Earlier conversation") or content.startswith("[Conversation compacted"):
                priority = 4  # compact block
            elif content.startswith("[REFLECTION]"):
                priority = 4  # reflection
            elif content.startswith("[Previous session context"):
                priority = 4  # session summary
            elif content.startswith("[Project Context"):
                priority = 4  # project context
            else:
                priority = 5  # 用户原始输入
        elif role == "assistant":
            if msg.get("tool_calls") or "Action:" in content:
                priority = 2  # 工具调用 (native or text)
            else:
                priority = 3  # 推理
        else:
            priority = 0

        # 近期消息加分
        recency_threshold = total - max(total // 4, 4)
        if index >= recency_threshold:
            priority += 2

        return priority

    @staticmethod
    def _build_native_pairs(messages: list[dict]) -> dict[int, int]:
        """
        构建 native tool_use 配对索引：tool_call assistant → tool result。

        Native 模式下 Anthropic/OpenAI 要求每个 tool_use 必须配对 tool_result，
        裁剪时必须原子操作——丢一个就丢一对。

        Returns:
            {assistant_idx: tool_result_idx, tool_result_idx: assistant_idx}
            双向映射，丢弃任何一方时必须同时丢弃另一方。
        """
        pairs: dict[int, int] = {}
        for i, msg in enumerate(messages):
            if msg.get("tool_calls"):
                # 找紧跟的 tool result（通常就是 i+1）
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id")
                    if not tc_id:
                        continue
                    for j in range(i + 1, min(i + 3, len(messages))):
                        if messages[j].get("tool_call_id") == tc_id:
                            pairs[i] = j
                            pairs[j] = i
                            break
        return pairs

    @staticmethod
    def _trim_by_priority(
        messages: list[dict],
        token_counts: list[int],
        token_limit: int,
    ) -> list[dict] | None:
        """
        第 0 级：按消息优先级裁剪。
        先丢弃低优先级的旧消息（优先级低 + 位置靠前的先丢）。
        Native tool_use 配对消息原子丢弃。
        """
        total_messages = len(messages)
        pairs = TokenBudget._build_native_pairs(messages)

        # 构建 (index, priority) 对，按 priority 升序 + index 升序排列
        indexed = [
            (i, TokenBudget._message_priority(messages[i], i, total_messages), token_counts[i])
            for i in range(total_messages)
        ]

        # 按优先级升序排列（低优先级在前，优先被丢弃）
        indexed.sort(key=lambda x: (x[1], x[0]))

        total_tokens = sum(token_counts)
        need_to_drop = total_tokens - token_limit
        dropped_tokens = 0
        drop_indices: set[int] = set()

        for idx, priority, tokens in indexed:
            if dropped_tokens >= need_to_drop:
                break
            # 不丢弃首条和优先级 >= 5 的消息
            if idx == 0 or priority >= 5:
                continue
            if idx in drop_indices:
                continue

            drop_indices.add(idx)
            dropped_tokens += tokens

            # 原子配对：丢弃 native tool_use 的另一半
            partner = pairs.get(idx)
            if partner is not None and partner not in drop_indices:
                drop_indices.add(partner)
                dropped_tokens += token_counts[partner]

        if not drop_indices:
            return None

        # 重建消息列表
        result = []
        for i, msg in enumerate(messages):
            if i not in drop_indices:
                result.append(msg)

        # 如果丢弃了消息，添加占位符
        if drop_indices:
            # 在首条之后插入提示
            placeholder = {
                "role": "user",
                "content": f"[{len(drop_indices)} low-priority messages trimmed to fit context]",
            }
            result.insert(1, placeholder)

        # 验证
        result_tokens = sum(_estimate_msg_tokens(m) for m in result)
        if result_tokens <= token_limit:
            return result
        return None

    @staticmethod
    def _trim_results_only(
        messages: list[dict],
        token_counts: list[int],
        token_limit: int,
    ) -> list[dict] | None:
        """
        第 1 级：丢弃旧的 observation。
        Text 模式：丢弃 [Tool: ...] user 消息，保留对应 assistant。
        Native 模式：原子丢弃 tool_call + tool_result 配对。
        从后往前处理，保留最近的消息。
        """
        first = messages[0]
        first_tokens = token_counts[0]
        pairs = TokenBudget._build_native_pairs(messages)

        # 从后往前选消息
        budget_left = token_limit - first_tokens
        drop_indices: set[int] = set()
        tool_result_count = 0

        for i in range(len(messages) - 1, 0, -1):
            msg = messages[i]
            tokens = token_counts[i]

            # 判断是否为 tool result（native role="tool" 或 text fallback [Tool: 开头）
            is_result = (
                msg.get("role") == "tool"
                or (
                    msg.get("role") == "user"
                    and _get_content_str(msg).strip().startswith("[Tool:")
                )
            )

            if is_result:
                tool_result_count += 1
                # Native 配对：计算丢弃整对的 token 成本
                partner = pairs.get(i)
                pair_tokens = tokens + (token_counts[partner] if partner is not None else 0)

                if budget_left >= pair_tokens:
                    # 能放下整对，保留
                    budget_left -= tokens
                else:
                    # 放不下，丢弃
                    drop_indices.add(i)
                    if partner is not None:
                        drop_indices.add(partner)
            else:
                if i in drop_indices:
                    continue
                if budget_left >= tokens:
                    budget_left -= tokens
                else:
                    drop_indices.add(i)
                    # 如果这是一个 native tool_call，也丢弃其配对
                    partner = pairs.get(i)
                    if partner is not None and partner not in drop_indices:
                        drop_indices.add(partner)

        if not drop_indices or tool_result_count == 0:
            return None

        # 重建消息列表
        selected = []
        for i in range(1, len(messages)):
            if i not in drop_indices:
                selected.append(messages[i])

        result = [first]
        if drop_indices:
            result.append({
                "role": "user",
                "content": (
                    f"[{len(drop_indices)} tool messages were removed "
                    f"to free context space]"
                ),
            })
        result.extend(selected)

        # 验证 token 预算
        if sum(_estimate_msg_tokens(m) for m in result) <= token_limit:
            return result
        return None

    @staticmethod
    def _trim_tool_calls(
        messages: list[dict],
        token_counts: list[int],
        token_limit: int,
    ) -> list[dict] | None:
        """
        第 2 级：丢弃旧的 tool_use（assistant 消息含 Action: 或 native tool_calls），
        仅保留 thought 部分。Native 配对消息原子操作。
        从后往前处理，保留最近的消息。
        """
        first = messages[0]
        first_tokens = token_counts[0]
        pairs = TokenBudget._build_native_pairs(messages)

        selected: list[dict] = []
        budget_left = token_limit - first_tokens
        dropped_calls = 0
        skip_indices: set[int] = set()

        for i in range(len(messages) - 1, 0, -1):
            if i in skip_indices:
                continue

            msg = messages[i]
            tokens = token_counts[i]
            content = _get_content_str(msg)

            # 跳过 native tool result（由其配对的 assistant 消息统一处理）
            if msg.get("role") == "tool" and msg.get("tool_call_id"):
                partner = pairs.get(i)
                if partner is not None:
                    # 将由 partner（assistant）的循环迭代统一处理
                    continue
                # 孤立的 tool result（不应该出现，但安全处理）
                if budget_left >= tokens:
                    selected.append(msg)
                    budget_left -= tokens
                else:
                    dropped_calls += 1
                continue

            # 判断是否为 tool call（native tool_calls 或 text fallback Action:）
            is_tool_call = (
                msg.get("role") == "assistant"
                and (msg.get("tool_calls") or "Action:" in content)
            )

            if is_tool_call:
                # Native 模式：计算整对的 token 成本
                partner = pairs.get(i)
                pair_tokens = tokens + (token_counts[partner] if partner is not None else 0)

                if budget_left >= pair_tokens:
                    # 整对放得下
                    selected.append(msg)
                    budget_left -= tokens
                    if partner is not None:
                        selected.append(messages[partner])
                        budget_left -= token_counts[partner]
                        skip_indices.add(partner)
                else:
                    # 放不下整对，尝试只保留 thought
                    thought = TokenBudget._extract_thought(msg)
                    if thought:
                        thought_tokens = estimate_tokens(thought)
                        if budget_left >= thought_tokens:
                            selected.append({"role": "assistant", "content": thought})
                            budget_left -= thought_tokens
                            dropped_calls += 1
                            # 配对的 tool result 也丢弃
                            if partner is not None:
                                skip_indices.add(partner)
                                dropped_calls += 1
                            continue
                    # thought 也放不下，整对丢弃
                    dropped_calls += 1
                    if partner is not None:
                        skip_indices.add(partner)
                        dropped_calls += 1
            else:
                if budget_left >= tokens:
                    selected.append(msg)
                    budget_left -= tokens
                else:
                    dropped_calls += 1

        if dropped_calls == 0:
            return None

        # 检查是否真的有 tool call 被压缩了
        tool_call_condensed = any(
            msg.get("role") == "assistant"
            and (msg.get("tool_calls") or "Action:" in _get_content_str(msg))
            for msg in selected
        )
        if not tool_call_condensed:
            return None

        selected.reverse()
        result = [first]
        result.extend(selected)

        if sum(_estimate_msg_tokens(m) for m in result) <= token_limit:
            return result
        return None

    @staticmethod
    def _trim_drop_all_but_last(
        messages: list[dict],
        token_limit: int,
        keep: int = 3,
    ) -> list[dict]:
        """
        第 3 级：兜底策略。保留首条 + 最后 keep 条消息。
        """
        first = messages[0]
        first_tokens = estimate_tokens(first.get("content", ""))
        last_keep = messages[-keep:] if len(messages) > keep + 1 else messages[1:]

        placeholder = {
            "role": "user",
            "content": (
                f"[{len(messages) - 1 - len(last_keep)} earlier messages "
                f"were truncated to fit context window]"
            ),
        }
        placeholder_tokens = estimate_tokens(placeholder["content"])

        result = [first, placeholder]
        budget_left = token_limit - first_tokens - placeholder_tokens

        for msg in last_keep:
            tokens = estimate_tokens(msg.get("content", ""))
            if budget_left >= tokens:
                result.append(msg)
                budget_left -= tokens
            else:
                break

        return result

    @staticmethod
    def _trim_simple(
        messages: list[dict],
        token_counts: list[int],
        token_limit: int,
    ) -> list[dict]:
        """回退策略：和原始实现一致，保留首条 + 尽量多最近消息。"""
        if not messages:
            return messages

        result = [messages[0]]
        remaining_budget = token_limit - token_counts[0]
        dropped = 0
        selected: list[dict] = []

        for msg, tokens in zip(reversed(messages[1:]), reversed(token_counts[1:])):
            if remaining_budget - tokens >= 0:
                selected.append(msg)
                remaining_budget -= tokens
            else:
                dropped += 1

        selected.reverse()
        if dropped > 0:
            result.append({
                "role": "user",
                "content": f"[{dropped} earlier messages were truncated to fit context window]",
            })
        result.extend(selected)
        return result

    @staticmethod
    def _extract_thought(msg: "dict | str") -> str | None:
        """从 assistant 消息中提取 thought 部分。

        Native 模式：content 就是 thought（tool_calls 单独存储）。
        Text 模式：Action: 之前的内容。
        """
        if isinstance(msg, dict):
            # Native tool_calls 模式：content 本身就是 thought
            if msg.get("tool_calls"):
                content = _get_content_str(msg)
                if content and not content.startswith("[Earlier"):
                    return content
                return None
            content = _get_content_str(msg)
        else:
            content = msg

        idx = content.find("Action:")
        if idx == -1:
            thought = content
        else:
            thought = content[:idx].strip()
        if thought and not thought.startswith("[Earlier"):
            return thought
        return None

    def usage_report(
        self,
        system_text: str,
        repo_map_text: str,
        history: list[dict],
        observation_text: str,
        consumed_tokens: int = 0,
        max_context_window: int | None = None,
    ) -> dict[str, int]:
        plan = self.compute_plan(consumed_tokens, max_context_window)
        history_tokens = sum(_estimate_msg_tokens(m) for m in history)
        return {
            "system":      estimate_tokens(system_text),
            "repo_map":    estimate_tokens(repo_map_text),
            "history":     history_tokens,
            "observation": estimate_tokens(observation_text),
            "total": (
                estimate_tokens(system_text)
                + estimate_tokens(repo_map_text)
                + history_tokens
                + estimate_tokens(observation_text)
            ),
            "budget":        self._total,
            "tiktoken_used": is_tiktoken_available(),
        }