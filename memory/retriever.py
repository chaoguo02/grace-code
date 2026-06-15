"""
memory/retriever.py

ProactiveRetriever — 每轮对话自动检索相关记忆并格式化为上下文片段。

工作流程：
  1. agent/core.py 在构建消息前调用 memory_context.set_user_message(msg)
  2. MemoryContext.build_memory_section() 调用 retriever.retrieve(msg)
  3. retriever 对 msg 做 search_chunks → 返回格式化文本注入 system prompt
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memory.external_store import ExternalMemoryStore

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CHUNKS = 5
_DEFAULT_MAX_TOKENS = 2000
_DEFAULT_MIN_SCORE = 0.35


class ProactiveRetriever:
    """
    主动检索器：根据用户消息语义搜索相关记忆 chunks。

    Args:
        external_store: ExternalMemoryStore 实例
        max_chunks: 最多返回几个 chunk
        max_tokens: 注入上下文的 token 上限（粗估 1 token ≈ 3 字符）
        min_score: 最低相关度阈值
    """

    def __init__(
        self,
        external_store: ExternalMemoryStore,
        max_chunks: int = _DEFAULT_MAX_CHUNKS,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        min_score: float = _DEFAULT_MIN_SCORE,
    ) -> None:
        self._store = external_store
        self._max_chunks = max_chunks
        self._max_tokens = max_tokens
        self._min_score = min_score

    def retrieve(
        self,
        user_message: str,
        task_description: str = "",
    ) -> list[dict]:
        """
        根据用户消息检索相关 chunks。

        Args:
            user_message: 当前用户消息
            task_description: 可选的任务描述（拼接到 query 提升检索质量）

        Returns:
            chunk 列表 [{"source_name", "content", "score", ...}]
        """
        if not user_message or not user_message.strip():
            return []

        query = user_message.strip()
        if task_description:
            query = f"{task_description}\n{query}"

        try:
            results = self._store.search_chunks(
                query=query,
                top_k=self._max_chunks * 2,
                min_score=self._min_score,
                max_per_source=2,
            )
        except Exception as exc:
            logger.warning("Proactive retrieval failed: %s", exc)
            return []

        # 按 token 预算截断
        selected: list[dict] = []
        total_chars = 0
        char_budget = self._max_tokens * 3  # 粗估

        for chunk in results:
            chunk_chars = len(chunk["content"]) + len(chunk["source_name"]) + 20
            if total_chars + chunk_chars > char_budget:
                break
            selected.append(chunk)
            total_chars += chunk_chars
            if len(selected) >= self._max_chunks:
                break

        return selected

    def format_for_injection(self, chunks: list[dict]) -> str:
        """
        将检索到的 chunks 格式化为可注入 system prompt 的文本。

        Returns:
            格式化的 markdown 文本，空列表返回空字符串
        """
        if not chunks:
            return ""

        lines: list[str] = []
        lines.append("## Relevant Memory Content")
        lines.append("")
        lines.append("The following memory fragments may be relevant to the current conversation:")
        lines.append("")

        for i, chunk in enumerate(chunks, 1):
            source = chunk["source_name"]
            content = chunk["content"].strip()
            score = chunk.get("score", 0)
            lines.append(f"### [{source}] (relevance: {score:.2f})")
            lines.append("")
            lines.append(content)
            lines.append("")

        return "\n".join(lines)
