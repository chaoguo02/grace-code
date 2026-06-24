"""
tools/file_edit_tool.py

file_edit 工具：基于 old_str/new_str 的精确字符串替换。

设计参考 Claude Code 的 Edit 工具语义：
- old_str 必须在文件中唯一匹配（0 匹配或多匹配都报错）
- new_str 替换该唯一匹配
- old_str 为空 + 文件不存在 → 创建新文件
- 不做全文覆盖，从根本上避免文件截断灾难
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.base import BaseTool, RiskLevel, ToolResult


class FileEditTool(BaseTool):
    """
    精确替换文件中的一段文本。

    params:
        path (str):    文件路径
        old_str (str): 要替换的精确字符串（必须在文件中唯一出现）
        new_str (str): 替换后的字符串
    """

    @property
    def name(self) -> str:
        return "file_edit"

    @property
    def risk_level(self) -> str:
        return RiskLevel.MEDIUM

    @property
    def description(self) -> str:
        return (
            "Replace one exact string occurrence in a file with new content. "
            "old_str must match exactly ONE location in the file (including whitespace and indentation). "
            "If old_str is empty and the file does not exist, creates a new file with new_str as content. "
            "Use this instead of file_write to modify existing files — it prevents accidental truncation."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to edit (absolute or relative to repo root)",
                },
                "old_str": {
                    "type": "string",
                    "description": (
                        "The exact string to find and replace. Must match exactly one location "
                        "in the file, including whitespace and indentation. "
                        "Include 3-5 lines of context to ensure uniqueness. "
                        "If empty, creates a new file (file must not already exist)."
                    ),
                },
                "new_str": {
                    "type": "string",
                    "description": (
                        "The replacement string. If empty, the old_str is deleted from the file."
                    ),
                },
            },
            "required": ["path", "old_str", "new_str"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        path = Path(params.get("path", ""))
        old_str: str = params.get("old_str", "")
        new_str: str = params.get("new_str", "")

        if not str(path):
            return ToolResult(success=False, output="", error="path is required")

        # Case 1: old_str 为空 → 创建新文件模式
        if not old_str:
            if path.exists():
                return ToolResult(
                    success=False,
                    output="",
                    error=(
                        f"File already exists: {path}. "
                        "Cannot use empty old_str on existing file. "
                        "Provide old_str to edit, or use file_write to overwrite."
                    ),
                )
            if not new_str:
                return ToolResult(
                    success=False,
                    output="",
                    error="Both old_str and new_str are empty. Nothing to do.",
                )
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(new_str, encoding="utf-8")
            except OSError as e:
                return ToolResult(success=False, output="", error=str(e))
            line_count = new_str.count("\n") + (1 if new_str and not new_str.endswith("\n") else 0)
            return ToolResult(
                success=True,
                output=f"Created new file: {path} ({line_count} lines)",
            )

        # Case 2: 文件必须存在
        if not path.exists():
            return ToolResult(
                success=False,
                output="",
                error=f"File not found: {path}. Cannot edit a non-existent file.",
            )
        if not path.is_file():
            return ToolResult(success=False, output="", error=f"Not a file: {path}")

        try:
            content = path.read_text(encoding="utf-8")
        except OSError as e:
            return ToolResult(success=False, output="", error=str(e))

        # Case 3: 计算匹配数
        count = content.count(old_str)

        if count == 0:
            # 提供有用的诊断信息
            stripped_count = content.count(old_str.strip())
            hint = ""
            if stripped_count > 0:
                hint = " (found matches ignoring leading/trailing whitespace — check indentation)"
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"old_str not found in {path}.{hint} "
                    "Make sure old_str matches the file content exactly, "
                    "including whitespace, indentation, and line endings."
                ),
            )

        if count > 1:
            # 显示匹配位置帮助 LLM 定位
            lines = content.split("\n")
            match_lines = []
            search_pos = 0
            for _ in range(min(count, 5)):
                idx = content.index(old_str, search_pos)
                line_num = content[:idx].count("\n") + 1
                match_lines.append(str(line_num))
                search_pos = idx + 1
            locations = ", ".join(match_lines)
            if count > 5:
                locations += f" ... and {count - 5} more"
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"old_str matches {count} locations in {path} (lines: {locations}). "
                    "Include more surrounding context in old_str to make it unique."
                ),
            )

        # Case 4: 唯一匹配 → 执行替换
        match_pos = content.index(old_str)
        start_line = content[:match_pos].count("\n") + 1

        new_content = content.replace(old_str, new_str, 1)

        try:
            path.write_text(new_content, encoding="utf-8")
        except OSError as e:
            return ToolResult(success=False, output="", error=str(e))

        old_lines = old_str.count("\n") + 1
        new_lines = new_str.count("\n") + 1
        delta = new_lines - old_lines

        delta_str = ""
        if delta > 0:
            delta_str = f" (+{delta} lines)"
        elif delta < 0:
            delta_str = f" ({delta} lines)"

        total_lines = new_content.count("\n") + (1 if new_content and not new_content.endswith("\n") else 0)

        return ToolResult(
            success=True,
            output=(
                f"Edited {path} at line {start_line}: "
                f"replaced {old_lines} lines with {new_lines} lines{delta_str}. "
                f"File now has {total_lines} lines total."
            ),
        )
