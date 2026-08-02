"""
tools/rag_tool.py

语义代码搜索工具（search_code）。

基于 AST 代码分块 + 向量索引，支持自然语言搜索代码片段。
与 search_text（正则 grep）互补：
- search_text: 知道确切文本/符号名时使用
- search_code: 知道语义意图但不确定具体实现在哪里时使用
"""

from __future__ import annotations

from typing import Any

from core.base import BaseTool, ToolResult


class SearchCodeTool(BaseTool):
    """
    语义代码搜索：用自然语言查找代码片段。

    params:
        query (str):         自然语言搜索查询
        file_pattern (str):  可选，限定文件类型（如 "*.py", "src/**"）
        top_k (int):         返回数量（默认 5）
    """

    def isReadOnly(self, params: dict[str, Any] | None = None) -> bool:
        return True

    def __init__(self, retriever=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._retriever = retriever

    @property
    def name(self) -> str:
        return "search_code"

    @property
    def description(self) -> str:
        return (
            "Semantic code search: find code snippets by natural language query. "
            "Use when you know WHAT you're looking for conceptually but not WHERE it is. "
            "Returns file paths, line numbers, symbol names, and code previews. "
            "Complements search_text (regex/literal) for cases where you don't know the exact text."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query (e.g. 'handle user authentication', 'parse JSON config')",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Optional file path glob filter (e.g. '*.py', 'src/**/*.ts')",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results to return (default: 5, max: 20)",
                },
            },
            "required": ["query"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        query = params.get("query", "").strip()
        if not query:
            return ToolResult(
                status="error",
                output="",
                error="query parameter is required",
            )

        if self._retriever is None:
            return ToolResult(
                status="error",
                output="",
                error="Code index not available. Run /reindex to build the code index first.",
            )

        file_pattern = params.get("file_pattern")
        top_k = min(params.get("top_k", 5), 20)

        try:
            results = self._retriever.search(
                query=query,
                top_k=top_k,
                file_pattern=file_pattern,
            )
        except Exception as e:
            return ToolResult(
                status="error",
                output="",
                error=f"Code search failed: {e}",
            )

        if not results:
            return ToolResult(
                status="success",
                output=f"No code matches found for: {query}",
            )

        output = self._format_results(results)
        return ToolResult(status="success", output=output)

    def _format_results(self, results: list[dict[str, Any]]) -> str:
        """格式化搜索结果为 agent 可读文本。"""
        lines = [f"Found {len(results)} matching code snippets:\n"]

        for i, r in enumerate(results, 1):
            score = r.get("score", 0)
            lines.append(
                f"--- [{i}] {r['file_path']}:{r['start_line']}-{r['end_line']} "
                f"| {r['symbol_kind']} `{r['symbol_name']}` | score: {score:.2f}"
            )
            if r.get("docstring"):
                lines.append(f"    Docstring: {r['docstring'][:150]}")

            # 代码预览（前 15 行）
            content = r.get("content", "")
            preview = content.splitlines()[:15]
            lines.extend(f"    {line}" for line in preview)
            if len(content.splitlines()) > 15:
                lines.append("    ... (truncated, use file_read for full content)")
            lines.append("")

        return "\n".join(lines)
