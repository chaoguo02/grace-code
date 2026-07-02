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
    from llm.base import LLMBackend
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
        selector_backend: "LLMBackend | None" = None,
    ) -> None:
        self._store = store
        self._max_lines = max_lines
        self._enabled = enabled
        self._task_context: str = ""
        self._retriever = retriever
        self._selector_backend = selector_backend
        self._user_message: str = ""
        self._cached_section: str | None = None
        self._already_surfaced: set[str] = set()
        self._recent_tools: list[str] = []

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
        if message != self._user_message:
            self._user_message = message
            self._cached_section = None  # invalidate step-level cache

    def add_recent_tool(self, tool_name: str) -> None:
        """Track recently used tools (for selector context hint)."""
        if tool_name not in self._recent_tools:
            self._recent_tools.append(tool_name)
            if len(self._recent_tools) > 10:
                self._recent_tools = self._recent_tools[-10:]

    def build_memory_section(self) -> str:
        """
        构建 Memory Section 文本。

        Injection strategy (aligned with Claude Code):
        - user/feedback types: always inject full content (they're short rules)
        - project/reference types: selected via Sonnet selector (max 5), or keyword fallback

        返回格式：
            ## Always-Loaded Memories (user/feedback)
            <full content of user and feedback memories>

            ## Selected Memories (project/reference)
            <full content of selected on-demand memories>

            ## Available Memories
            <index listing for remaining memories>

        没有记忆时返回空字符串。
        """
        if not self._enabled:
            return ""

        if self._cached_section is not None:
            return self._cached_section

        parts: list[str] = []

        # 1. Always-inject: full content of user/feedback memories
        always_section = self._build_always_inject_section()
        if always_section:
            parts.append(always_section)

        # 2. On-demand: Sonnet selector or keyword fallback for project/reference
        selected_section = self._build_selected_section()
        if selected_section:
            parts.append(selected_section)

        # 3. Index listing (for remaining memories the LLM can read on demand)
        if self._task_context:
            index_section = self._build_filtered_section()
        else:
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
        if index_section:
            parts.append(index_section)

        # 4. RAG 主动检索
        rag_section = self._build_rag_section()
        if rag_section:
            parts.append(rag_section)

        self._cached_section = "\n\n".join(parts)
        return self._cached_section

    def _build_always_inject_section(self) -> str:
        """Load full content of all user/feedback memories (always injected)."""
        from memory.models import ALWAYS_INJECT_TYPES
        summaries = self._store.list_memories()
        always_mems = [s for s in summaries if s.type in ALWAYS_INJECT_TYPES]
        if not always_mems:
            return ""

        lines: list[str] = ["## Active Rules & Preferences"]
        for s in always_mems:
            try:
                mem = self._store.read_memory(s.name)
                if mem and mem.content.strip():
                    lines.append(f"### {s.name} ({s.type})")
                    lines.append(mem.content.strip())
                    lines.append("")
            except Exception:
                continue

        if len(lines) <= 1:
            return ""
        return "\n".join(lines)

    def _build_selected_section(self) -> str:
        """Select and load on-demand (project/reference) memories via Sonnet selector."""
        query = self._user_message or self._task_context
        if not query:
            return ""

        # Try Sonnet selector first
        if self._selector_backend:
            from memory.selector import select_memories
            selected_names = select_memories(
                query=query,
                memory_dir=self._store.store_dir,
                selector_backend=self._selector_backend,
                already_surfaced=self._already_surfaced,
                recent_tools=self._recent_tools,
            )
            if selected_names:
                return self._load_selected_memories(selected_names)

        # Fallback: no selector configured or selector returned nothing
        return ""

    def _load_selected_memories(self, names: list[str]) -> str:
        """Load full content of selected memories, tracking what was surfaced."""
        from agent.v2.runtime import SessionRuntime
        lines: list[str] = ["## Selected Project Knowledge"]
        loaded = 0
        for name in names:
            if name in self._already_surfaced:
                continue
            mem = self._store.read_memory(name)
            if mem and mem.content.strip():
                freshness = SessionRuntime._memory_freshness_text(name, self._store)
                lines.append(f"### {name}")
                lines.append(mem.content.strip())
                if freshness:
                    lines.append(f"\n> ⚠️ {freshness}")
                lines.append("")
                self._already_surfaced.add(name)
                loaded += 1
        if loaded == 0:
            return ""
        return "\n".join(lines)

    def _build_rag_section(self) -> str:
        """用 ProactiveRetriever 检索相关 chunks 并格式化。

        按类型分配检索配额：
        - project: top-5（稳定项目知识）
        - reference: top-3（外部资源指引）
        - feedback 不在此注入（通过 task anchor 按文件触发）
        """
        if not self._retriever:
            return ""
        query = self._user_message or self._task_context
        if not query:
            return ""
        try:
            chunks = self._retriever.retrieve(
                user_message=query,
                task_description=self._task_context,
            )
            project_chunks: list[dict] = []
            reference_chunks: list[dict] = []
            other_chunks: list[dict] = []
            for chunk in chunks:
                mem_type = (chunk.get("metadata") or {}).get("type", "")
                if mem_type == "project" and len(project_chunks) < 5:
                    project_chunks.append(chunk)
                elif mem_type == "reference" and len(reference_chunks) < 3:
                    reference_chunks.append(chunk)
                elif mem_type not in ("project", "reference", "feedback"):
                    other_chunks.append(chunk)
            filtered = project_chunks + reference_chunks + other_chunks
            return self._retriever.format_for_injection(filtered)
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
            # feedback 规则优先展示，避免任务约束被项目记忆淹没。
            if mem.type == "feedback":
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
            self._append_grouped_memories(lines, [mem for _score, mem in relevant])

        if other:
            lines.append("### Other memories")
            other_memories = [mem for _score, mem in other]
            shown_count = self._append_grouped_memories(lines, other_memories, limit=10)
            if len(other_memories) > shown_count:
                lines.append(f"  ... and {len(other_memories) - shown_count} more")

        lines.append("")
        lines.append("Use memory_read to read a specific memory, memory_write to")
        lines.append("save new information you want to remember across sessions.")

        # 按行数限制
        result = "\n".join(lines)
        result_lines = result.splitlines()
        if len(result_lines) > self._max_lines:
            result = "\n".join(result_lines[:self._max_lines])

        return result

    def get_feedback_for_files(
        self, accessed_files: set[str], *, record_access: bool = False,
    ) -> str:
        """
        根据已访问文件的锚点匹配，返回相关 feedback 记忆内容。

        feedback 规则嵌入 task anchor 每步注入，不会被 compaction 丢失。

        Args:
            accessed_files: 已访问的文件路径集合（相对路径）
            record_access: 是否递增匹配到的记忆的 access_count

        Returns:
            格式化的 feedback 记忆文本；无匹配时返回空字符串。
        """
        if not self._enabled or not accessed_files:
            return ""

        summaries = self._store.list_memories()
        feedback_mems = [s for s in summaries if s.type == "feedback"]
        if not feedback_mems:
            return ""

        normalized_files = {
            p.replace("\\", "/").lstrip("./") for p in accessed_files
        }

        matched_memories: list[str] = []
        matched_names: list[str] = []
        for mem_summary in feedback_mems:
            mem = self._store.read_memory(mem_summary.name)
            if mem is None:
                continue
            for anchor in mem.anchors:
                if anchor.kind != "file" or not anchor.path:
                    continue
                anchor_path = anchor.path.replace("\\", "/").lstrip("./")
                for f in normalized_files:
                    if f == anchor_path or f.startswith(anchor_path + "/"):
                        stale_warn = ""
                        if mem.metadata.stale:
                            stale_warn = "\n> **⚠ STALE**: This rule may be outdated — the anchored file was modified since this memory was created."
                        matched_memories.append(
                            f"### {mem.name}\n{mem.content.strip()}{stale_warn}"
                        )
                        matched_names.append(mem.name)
                        break
                else:
                    continue
                break

        if not matched_memories:
            return ""

        if record_access:
            for name in matched_names:
                self._store.record_access(name)

        return "\n\n".join([
            "## Feedback Rules (triggered by file access)",
            *matched_memories,
        ])

    def get_procedural_for_files(
        self, accessed_files: set[str], *, record_access: bool = False,
    ) -> str:
        """Backward-compatible alias for get_feedback_for_files()."""
        return self.get_feedback_for_files(accessed_files, record_access=record_access)

    @staticmethod
    def _append_grouped_memories(lines: list[str], memories: list[object], limit: int | None = None) -> int:
        """按类型优先级输出记忆摘要，feedback 始终在最前。"""
        groups = [
            ("Rules to follow", "feedback"),
            ("Project knowledge", "project"),
            ("About the user", "user"),
            ("References", "reference"),
        ]
        shown = 0
        known_types = {t for _, t in groups}
        for title, mem_type in groups:
            typed = [mem for mem in memories if getattr(mem, "type", "") == mem_type]
            if limit is not None:
                typed = typed[:max(0, limit - shown)]
            if not typed:
                continue
            lines.append(f"#### {title}")
            for mem in typed:
                lines.append(f"- [{mem.name}]({mem.name}.md) — {mem.description} ({mem.type})")
                shown += 1
                if limit is not None and shown >= limit:
                    return shown
        remaining = [mem for mem in memories if getattr(mem, "type", "") not in known_types]
        for mem in remaining:
            if limit is not None and shown >= limit:
                break
            lines.append(f"- [{mem.name}]({mem.name}.md) — {mem.description} ({mem.type})")
            shown += 1
        return shown
