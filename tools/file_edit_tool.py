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

import os as _os
from pathlib import Path
from typing import Any, TYPE_CHECKING

from tools.base import (
    BaseTool, PathAccess, RiskLevel, ToolEffect, ToolMetadata, ToolResult,
)

if TYPE_CHECKING:
    from tools.file_tool import FileReadCache


class FileEditTool(BaseTool):
    """Precise string replacement, aligned with Claude Code Edit tool.

    Claude Code pattern: three checks before applying an edit:
    1. Read-before-Edit: must have read the file in this conversation
    2. Match: old_string must appear exactly
    3. Uniqueness: old_string must appear exactly once (or replace_all=True)
    """
    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.WRITE_WORKSPACE}),
        path_access=PathAccess.WRITE,
        path_parameter="path",
    )
    """
    精确替换文件中的一段文本。

    params:
        path (str):    文件路径
        old_str (str): 要替换的精确字符串（必须在文件中唯一出现）
        new_str (str): 替换后的字符串
    """

    def __init__(self, read_cache: "FileReadCache | None" = None,
                 workspace_root: str | None = None) -> None:
        self._read_cache = read_cache
        self._workspace_root = workspace_root

    aliases = ("file_edit",)

    @property
    def name(self) -> str:
        return "Edit"

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

        ws = self._workspace_root

        # ── Layer 1: Sanitize path ──
        if ws is not None:
            from tools.base import sanitize_path
            try:
                clean = sanitize_path(str(path), ws)
            except ValueError as e:
                return ToolResult(success=False, output="", error=str(e))
            path = Path(clean)

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
            if ws is not None:
                from tools.base import resolve_safe_parent, safe_create_file
                safe_path, err = resolve_safe_parent(str(path), ws)
                if err:
                    return ToolResult(success=False, output="", error=err)
                fd, err = safe_create_file(safe_path)
                if err:
                    return ToolResult(success=False, output="", error=err)
                _os.write(fd, new_str.encode("utf-8"))
                _os.close(fd)
                path = Path(safe_path)
            else:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(new_str, encoding="utf-8")
                except OSError as e:
                    return ToolResult(success=False, output="", error=str(e))
            line_count = new_str.count("\n") + (1 if new_str and not new_str.endswith("\n") else 0)
            if self._read_cache is not None:
                self._read_cache.invalidate(str(path.resolve()) if ws is None else str(path))
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

        # ── Write with O_NOFOLLOW (TOCTOU protection) ──
        if ws is not None:
            from tools.base import resolve_safe_parent, safe_open_for_write
            safe_path, err = resolve_safe_parent(str(path), ws)
            if err:
                return ToolResult(success=False, output="", error=err)
            fd, err = safe_open_for_write(safe_path)
            if err:
                return ToolResult(success=False, output="", error=err)
            _os.write(fd, new_content.encode("utf-8"))
            _os.close(fd)
            write_path = safe_path
        else:
            try:
                path.write_text(new_content, encoding="utf-8")
            except OSError as e:
                return ToolResult(success=False, output="", error=str(e))
            write_path = str(path.resolve())

        # ── Invalidate read cache for this path ──
        if self._read_cache is not None:
            self._read_cache.invalidate(write_path)

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
