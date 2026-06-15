"""
memory/context.py

MemoryContext — 管理记忆在 LLM 上下文中的注入。

记忆索引以独立的 user message 注入（不影响 system prompt 的 prompt cache），
在 compaction 后从 MemoryStore 重新读取以确保长对话不丢失长期记忆上下文。

支持相关性过滤：根据当前任务描述的关键词，优先展示相关记忆。
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import TYPE_CHECKING

from memory.store import MemoryStore

if TYPE_CHECKING:
    from memory.retriever import ProactiveRetriever

logger = logging.getLogger(__name__)

# 停用词（中英文常见词，不用于相关性匹配）
_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "to", "of", "in",
    "for", "on", "with", "at", "by", "from", "as", "into", "through",
    "and", "or", "but", "if", "not", "no", "this", "that", "it", "its",
    "all", "each", "every", "both", "few", "more", "most", "other",
    "的", "了", "是", "在", "有", "和", "就", "不", "人", "都", "一",
    "我", "你", "他", "她", "它", "们", "这", "那", "个", "中",
    "上", "下", "把", "让", "用", "到", "说", "也", "去", "能",
})

_WORD_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_-]*|[一-鿿]+")


def _extract_keywords(text: str) -> set[str]:
    """从文本中提取关键词（去除停用词，全部小写）。"""
    words = _WORD_RE.findall(text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


class MemoryContext:
    """
    管理记忆在 agent 上下文中的注入。

    职责：
    - 构建 Memory Section 文本（注入独立的 project context user message）
    - 每次构建时从 MemoryStore 重新读取（确保 compaction 后不丢失）
    - 按任务相关性过滤和排序记忆条目
    """

    def __init__(
        self,
        store: MemoryStore,
        max_lines: int = 50,
        enabled: bool = True,
        retriever: ProactiveRetriever | None = None,
    ) -> None:
        self._store = store
        self._max_lines = max_lines
        self._enabled = enabled
        self._task_context: str = ""
        self._retriever = retriever
        self._user_message: str = ""

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def store(self) -> MemoryStore:
        """访问底层 MemoryStore（用于 compaction 后重新加载）。"""
        return self._store

    def set_task_context(self, task_description: str) -> None:
        """设置当前任务描述，用于记忆相关性过滤。"""
        self._task_context = task_description

    def set_user_message(self, message: str) -> None:
        """设置当前轮用户消息，用于 RAG 主动检索。"""
        self._user_message = message

    def build_memory_section(self) -> str:
        """
        构建 Memory Section 文本。

        每次调用都从磁盘重新读取 MEMORY.md，确保：
        - compaction 后重新注入最新索引
        - agent 运行期间新写入的记忆能被感知

        当设置了 task_context 时，按相关性排序记忆条目，
        将最相关的记忆放在前面。

        当配置了 retriever 且有 user_message 时，附加 RAG 检索结果。

        返回格式：
            ## Available Memories
            <按相关性排序的 MEMORY.md 条目>

            ## Relevant Memory Content
            <RAG 检索到的 chunk 内容>

        没有记忆时返回空字符串。
        """
        if not self._enabled:
            return ""

        # 如果有任务上下文，使用相关性过滤
        if self._task_context:
            index_section = self._build_filtered_section()
        else:
            # 无任务上下文时，注入完整索引
            index_content = self._store.get_index_content(max_lines=self._max_lines)
            if not index_content.strip():
                index_section = ""
            else:
                index_section = "\n".join([
                    "## Available Memories",
                    index_content,
                    "",
                    "Use memory_read to read a specific memory, memory_write to",
                    "save new information you want to remember across sessions.",
                ])

        # RAG 主动检索
        rag_section = self._build_rag_section()

        parts = [p for p in (index_section, rag_section) if p]
        return "\n\n".join(parts)

    def _build_rag_section(self) -> str:
        """用 ProactiveRetriever 检索相关 chunks 并格式化。"""
        if not self._retriever or not self._user_message:
            return ""
        try:
            chunks = self._retriever.retrieve(
                user_message=self._user_message,
                task_description=self._task_context,
            )
            return self._retriever.format_for_injection(chunks)
        except Exception as exc:
            logger.debug("RAG retrieval failed: %s", exc)
            return ""

    def _build_filtered_section(self) -> str:
        """按相关性过滤和排序记忆条目。"""
        summaries = self._store.list_memories()
        if not summaries:
            return ""

        task_keywords = _extract_keywords(self._task_context)
        if not task_keywords:
            # 无可提取的关键词，退回完整索引
            index_content = self._store.get_index_content(max_lines=self._max_lines)
            if not index_content.strip():
                return ""
            return "\n".join([
                "## Available Memories",
                index_content,
                "",
                "Use memory_read to read a specific memory, memory_write to",
                "save new information you want to remember across sessions.",
            ])

        # 计算每条记忆的相关性得分
        scored: list[tuple[float, object]] = []
        for mem in summaries:
            mem_keywords = _extract_keywords(f"{mem.name} {mem.description}")
            overlap = task_keywords & mem_keywords
            score = len(overlap)
            # feedback 和 user 类型加权（始终相关）
            if mem.type in ("feedback", "user"):
                score += 0.5
            scored.append((score, mem))

        # 按得分降序排列
        scored.sort(key=lambda x: x[0], reverse=True)

        # 相关记忆（得分 > 0）放前面，无关记忆简要列出
        relevant = [(s, m) for s, m in scored if s > 0]
        other = [(s, m) for s, m in scored if s == 0]

        lines = ["## Available Memories"]

        if relevant:
            lines.append("### Relevant to current task")
            for _score, mem in relevant:
                lines.append(f"- [{mem.name}]({mem.name}.md) — {mem.description} ({mem.type})")

        if other:
            lines.append("### Other memories")
            for _score, mem in other[:10]:
                lines.append(f"- [{mem.name}]({mem.name}.md) — {mem.description} ({mem.type})")
            if len(other) > 10:
                lines.append(f"  ... and {len(other) - 10} more")

        lines.append("")
        lines.append("Use memory_read to read a specific memory, memory_write to")
        lines.append("save new information you want to remember across sessions.")

        # 按行数限制
        result = "\n".join(lines)
        result_lines = result.splitlines()
        if len(result_lines) > self._max_lines:
            result = "\n".join(result_lines[:self._max_lines])

        return result
