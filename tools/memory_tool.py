"""
tools/memory_tool.py

记忆读写工具，让 agent 在对话中读写长期记忆。

四个工具：
- memory_read:   读取一条记忆
- memory_write:  创建或更新一条记忆
- memory_list:   列出所有记忆摘要
- memory_delete: 删除一条记忆

这些工具通过 ToolRegistry 注册，和其他工具（web_search、file_read 等）平级。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from tools.base import BaseTool, ToolResult

if TYPE_CHECKING:
    from memory.store import MemoryStore
    from memory.models import Memory, MemoryMetadata
    from memory.external_store import ExternalMemoryStore

logger = logging.getLogger(__name__)

# 可选类型字面量，帮助 LLM 决定用什么类型
_TYPE_DESCRIPTIONS = {
    "user": "Personal preferences, workflow habits, tool choices",
    "feedback": "Corrections, lessons learned, things to avoid",
    "project": "Build commands, architecture, conventions, project-specific knowledge",
    "reference": "External docs, links, general reference information",
}


# ---------------------------------------------------------------------------
# MemoryReadTool
# ---------------------------------------------------------------------------

class MemoryReadTool(BaseTool):
    """
    读取一条记忆。按 name（短横线 slug）查找并返回完整内容。
    常用于 memory_list 之后读取具体内容。
    """

    def __init__(self, store: "MemoryStore") -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "memory_read"

    @property
    def description(self) -> str:
        return (
            "Read a specific saved memory by name. Returns the full content of the memory. "
            "Use this after memory_list to read details of a relevant memory. "
            "The name is a short kebab-case slug like 'build-commands' or 'debugging-tips'."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the memory to read (kebab-case slug, e.g. 'build-commands')",
                },
            },
            "required": ["name"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        name: str = (params.get("name") or "").strip()
        if not name:
            return ToolResult(success=False, output="", error="name is required")

        memory = self._store.read_memory(name)
        if memory is None:
            return ToolResult(
                success=False,
                output="",
                error=f"Memory '{name}' not found. Use memory_list to see available memories.",
            )

        return ToolResult(success=True, output=memory.content)


# ---------------------------------------------------------------------------
# MemoryWriteTool
# ---------------------------------------------------------------------------

class MemoryWriteTool(BaseTool):
    """
    创建或更新一条记忆。自动更新 MEMORY.md 索引。
    当 agent 发现值得跨会话记住的信息时使用（构建命令、用户偏好、调试技巧等）。
    """

    def __init__(self, store: "MemoryStore") -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "memory_write"

    @property
    def description(self) -> str:
        return (
            "Save information to persistent memory. The memory will be available in future sessions. "
            "Use this to remember build commands, test setup, user preferences, debugging insights, "
            "architecture decisions, or any information that would be useful across conversations. "
            "If a memory with the same name already exists, it will be overwritten. "
            "After writing, use memory_list to confirm the index was updated."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Short kebab-case slug for the memory, e.g. 'build-commands' or "
                        "'api-conventions'. Use lowercase letters, numbers, and hyphens only."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "One-line summary of what this memory contains. Shown in memory_list output.",
                },
                "type": {
                    "type": "string",
                    "enum": ["user", "feedback", "project", "reference"],
                    "description": (
                        "Type of memory: user=personal preferences, "
                        "feedback=corrections/lessons, "
                        "project=build commands/architecture/conventions, "
                        "reference=external docs/links"
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "The memory content in Markdown format. Use headers, lists, and code blocks as needed.",
                },
            },
            "required": ["name", "description", "type", "content"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        name: str = (params.get("name") or "").strip()
        description: str = (params.get("description") or "").strip()
        mem_type: str = (params.get("type") or "reference").strip()
        content: str = (params.get("content") or "").strip()

        if not name:
            return ToolResult(success=False, output="", error="name is required")
        if not description:
            return ToolResult(success=False, output="", error="description is required")
        if not content:
            return ToolResult(success=False, output="", error="content is required")
        if mem_type not in _TYPE_DESCRIPTIONS:
            valid = ", ".join(_TYPE_DESCRIPTIONS.keys())
            return ToolResult(
                success=False, output="",
                error=f"Invalid type '{mem_type}'. Valid types: {valid}",
            )

        from memory.models import Memory, MemoryMetadata

        memory = Memory(
            name=name,
            description=description,
            content=content,
            metadata=MemoryMetadata(type=mem_type),
        )

        if self._store.write_memory(memory):
            return ToolResult(
                success=True,
                output=f"Memory '{name}' saved successfully (type: {mem_type}).",
            )
        else:
            return ToolResult(
                success=False, output="",
                error=f"Failed to write memory '{name}'",
            )


# ---------------------------------------------------------------------------
# MemoryListTool
# ---------------------------------------------------------------------------

class MemoryListTool(BaseTool):
    """
    列出所有记忆的摘要（名称 + 一行描述 + 类型）。
    agent 在开始任务前应调用此工具检查是否有相关记忆。
    """

    def __init__(self, store: "MemoryStore") -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "memory_list"

    @property
    def description(self) -> str:
        return (
            "List all saved memories with their name, description, and type. "
            "Use this at the start of a task to check if there is relevant prior knowledge. "
            "Then use memory_read to access a specific memory's full content."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": list(_TYPE_DESCRIPTIONS.keys()),
                    "description": "Optional filter by memory type",
                },
            },
            "required": [],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        filter_type: str | None = (params.get("type") or "").strip() or None
        if filter_type and filter_type not in _TYPE_DESCRIPTIONS:
            valid = ", ".join(_TYPE_DESCRIPTIONS.keys())
            return ToolResult(
                success=False, output="",
                error=f"Invalid type '{filter_type}'. Valid types: {valid}",
            )

        summaries = self._store.list_memories()
        if filter_type:
            summaries = [s for s in summaries if s.type == filter_type]

        if not summaries:
            msg = "No memories saved yet."
            if filter_type:
                msg = f"No memories of type '{filter_type}' found."
            return ToolResult(success=True, output=msg)

        lines = [f"Memories ({len(summaries)} total):\n"]
        for s in summaries:
            lines.append(f"  - {s.name}: {s.description} ({s.type})")
        lines.append("\nUse memory_read <name> to read full content.")

        return ToolResult(success=True, output="\n".join(lines))


# ---------------------------------------------------------------------------
# MemoryDeleteTool
# ---------------------------------------------------------------------------

class MemoryDeleteTool(BaseTool):
    """
    删除一条记忆。按 name 删除对应的记忆文件，并更新 MEMORY.md 索引。
    谨慎使用——删除不可恢复。
    """

    def __init__(self, store: "MemoryStore") -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "memory_delete"

    @property
    def description(self) -> str:
        return (
            "Delete a saved memory by name. This is permanent and cannot be undone. "
            "Use memory_list first to confirm the memory exists and you have the correct name."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the memory to delete (kebab-case slug)",
                },
            },
            "required": ["name"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        name: str = (params.get("name") or "").strip()
        if not name:
            return ToolResult(success=False, output="", error="name is required")

        if self._store.delete_memory(name):
            return ToolResult(
                success=True,
                output=f"Memory '{name}' deleted.",
            )
        else:
            return ToolResult(
                success=False, output="",
                error=f"Failed to delete memory '{name}'",
            )


# ---------------------------------------------------------------------------
# MemorySearchTool — 外部记忆语义搜索（ExternalMemoryStore）
# ---------------------------------------------------------------------------

class MemorySearchTool(BaseTool):
    """
    语义搜索外部记忆。返回按相关性排序的结果。
    和 memory_list（精准列出）互补，适合"记得有但想不起来叫什么"的场景。
    """

    def __init__(self, external_store: "ExternalMemoryStore | None" = None) -> None:
        self._store = external_store

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def description(self) -> str:
        return (
            "Search saved memories by semantic similarity to a natural language query. "
            "Returns results ranked by relevance (highest score first). "
            "Use this when you vaguely remember something but don't know the exact name. "
            "For exact listing by type, use memory_list instead."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum number of results (default 5, max 20)",
                },
            },
            "required": ["query"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        if self._store is None:
            return ToolResult(
                success=False, output="",
                error="External memory store is not available. "
                      "The memory_search tool requires an ExternalMemoryStore to be configured.",
            )

        query: str = (params.get("query") or "").strip()
        if not query:
            return ToolResult(success=False, output="", error="query is required")

        top_k: int = min(int(params.get("top_k", 5)), 20)

        # 优先使用 chunk 级别搜索（更精准）
        try:
            chunk_results = self._store.search_chunks(
                query=query, top_k=top_k, min_score=0.3, max_per_source=2,
            )
        except Exception:
            chunk_results = []

        if chunk_results:
            lines = [f"Search results for: {query}\n"]
            for i, r in enumerate(chunk_results, 1):
                score_pct = int(r["score"] * 100)
                lines.append(f"  {i}. [{r['source_name']}] (relevance: {score_pct}%)")
                preview = r["content"][:200].replace("\n", " ")
                if len(r["content"]) > 200:
                    preview += "..."
                lines.append(f"     {preview}")
                lines.append("")
            return ToolResult(success=True, output="\n".join(lines))

        # 降级到 memory 级别搜索
        try:
            results = self._store.search(query=query, top_k=top_k)
        except Exception as exc:
            logger.error("Memory search failed: %s", exc)
            return ToolResult(
                success=False, output="",
                error=f"Memory search failed: {exc}",
            )

        if not results:
            return ToolResult(
                success=True,
                output="No matching memories found.",
            )

        lines = [f"Search results for: {query}\n"]
        for i, r in enumerate(results, 1):
            score_pct = int(r["score"] * 100)
            lines.append(f"  {i}. [{r['name']}] (relevance: {score_pct}%)")
            preview = r["content"][:200].replace("\n", " ")
            if len(r["content"]) > 200:
                preview += "..."
            lines.append(f"     {preview}")
            lines.append("")

        return ToolResult(success=True, output="\n".join(lines))
