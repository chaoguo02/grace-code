"""
tools/file_tool.py

文件操作工具，提供三个 action：
- file_read:   读取文件全部内容
- file_view:   分窗口查看文件（防止一次读爆上下文）
- file_write:  写入文件（全量覆盖）

设计原则：
- file_read 对大文件做行数截断，超出时提示用 file_view 分页
- file_view 维护"窗口"概念，每次返回固定行数，agent 可 scroll
- file_write 写入前自动创建父目录，写入后返回行数确认
- 所有路径都限制在 repo_path 内（防止读取系统文件）
- FileReadCache: Claude Code 风格的工具层缓存，防止子代理重复读取
  同一文件。将"检测循环"转变为"预防重复"——在工具层就让重复读取
  无害化，而不是事后检测。
"""

from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

# 单次 file_read 最多返回的行数，超出提示用 file_view
MAX_READ_LINES = 500
# file_view 每窗口显示的行数
VIEW_WINDOW_LINES = 100
# 同一文件最多允许的读取次数（file_read + file_view 合计）
MAX_READS_PER_FILE = 3


# ═══════════════════════════════════════════════════════════════════════════
# FileReadCache — Claude Code style tool-layer dedup
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class _CacheEntry:
    """A single read range in the cache."""

    offset: int | None   # 1-indexed start line (None = full file)
    limit: int | None    # number of lines read (None = full file)
    end_line: int | None  # computed: offset + limit - 1 (None = full file)
    content: str


@dataclass
class FileReadCache:
    """Per-agent cache of file read results.

    Claude Code approach: prevent duplicate reads at the tool layer
    by caching content. When a re-read request overlaps with cached
    content, return the cached content directly instead of reading
    from disk. This makes repeated reads harmless rather than
    detecting them after the fact as a "loop."

    Scoped to a single agent run. In fork_subagent(), each subagent
    gets a FRESH cache — it does NOT inherit the parent's cache,
    because the subagent runs in an isolated context and should
    verify facts independently.
    """

    entries: dict[str, list[_CacheEntry]] = field(default_factory=dict)
    read_counts: dict[str, int] = field(default_factory=dict)

    def check(self, normalized_path: str, offset: int | None, limit: int | None) -> str | None:
        """Return cached content if the requested range is fully covered, or None."""
        entries = self.entries.get(normalized_path, [])
        if not entries:
            return None

        requested_start = offset if offset is not None else 1
        requested_end: int | None
        if offset is None and limit is None:
            requested_end = None  # asking for the full file
        elif limit is not None:
            requested_end = requested_start + limit - 1
        else:
            requested_end = None

        for entry in entries:
            if requested_end is None:
                # Asking for full file — check if any cached entry covers it
                if entry.end_line is None:
                    return entry.content
                continue
            if entry.end_line is None:
                # Entry has full file — always covers any sub-range
                return entry.content
            if entry.offset is not None and entry.end_line is not None:
                if entry.offset <= requested_start and entry.end_line >= requested_end:
                    return entry.content

        return None

    def store(
        self,
        normalized_path: str,
        offset: int | None,
        limit: int | None,
        content: str,
    ) -> None:
        """Record a read operation in the cache."""
        end_line: int | None
        if offset is None and limit is None:
            end_line = None  # full file
        elif limit is not None:
            start = offset if offset is not None else 1
            end_line = start + limit - 1
        else:
            end_line = None

        entry = _CacheEntry(
            offset=offset,
            limit=limit,
            end_line=end_line,
            content=content,
        )
        self.entries.setdefault(normalized_path, []).append(entry)

    def count_and_check(self, normalized_path: str) -> int:
        """Increment the read count for a file and return the new count.
        Returns -1 without incrementing if the cap has been reached."""
        current = self.read_counts.get(normalized_path, 0)
        if current >= MAX_READS_PER_FILE:
            return -1
        current += 1
        self.read_counts[normalized_path] = current
        return current

    def reset(self) -> None:
        """Clear all cached data. Called at the start of each subagent run."""
        self.entries.clear()
        self.read_counts.clear()


class FileReadTool(BaseTool):
    """
    读取文件内容。超过 MAX_READ_LINES 行时截断并提示。

    Uses FileReadCache to prevent repeated reads of the same file.
    Each agent (parent or subagent) gets its own cache instance.

    params:
        path (str): 文件路径（相对或绝对）
    """

    def __init__(self, read_cache: FileReadCache | None = None) -> None:
        self._read_cache = read_cache or FileReadCache()

    @property
    def name(self) -> str:
        return "file_read"

    @property
    def description(self) -> str:
        return (
            f"Read the contents of a file. "
            f"Files longer than {MAX_READ_LINES} lines will be truncated; "
            f"use file_view with line numbers to read specific sections."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read (absolute or relative to repo root)",
                },
            },
            "required": ["path"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        path = Path(params.get("path", ""))
        normalized = str(path.resolve())

        # ── Cache check (Claude Code style: prevent before detect) ──
        # file_read reads from line 1 up to MAX_READ_LINES
        cached = self._read_cache.check(normalized, offset=1, limit=MAX_READ_LINES)
        if cached is not None:
            count = self._read_cache.read_counts.get(normalized, 0) + 1
            self._read_cache.read_counts[normalized] = count
            logger.debug("file_read cache hit: %s (read #%d)", normalized, count)
            return ToolResult(
                success=True,
                output=(
                    f"{cached}\n\n"
                    f"[CACHED] This file content was already read in this run. "
                    f"(read #{count}/{MAX_READS_PER_FILE}). "
                    "Use the earlier observation instead of re-reading."
                ),
            )

        # ── Frequency cap ──
        count = self._read_cache.count_and_check(normalized)
        if count < 0:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"File '{path}' has already been read {MAX_READS_PER_FILE} times "
                    "in this run. Use the content you already have to complete your task."
                ),
            )

        # ── Actual read ──
        if not path.exists():
            return ToolResult(success=False, output="", error=f"File not found: {path}")
        if not path.is_file():
            return ToolResult(success=False, output="", error=f"Not a file: {path}")

        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            return ToolResult(success=False, output="", error=str(e))

        total = len(lines)
        truncated = total > MAX_READ_LINES
        display_lines = lines[:MAX_READ_LINES]

        numbered = "\n".join(
            f"{i + 1:4d} | {line}"
            for i, line in enumerate(display_lines)
        )

        suffix = ""
        if truncated:
            suffix = (
                f"\n... ({total - MAX_READ_LINES} more lines not shown) "
                f"Use file_view with start_line to read the rest."
            )

        output = f"File: {path} ({total} lines total)\n{numbered}{suffix}"

        # ── Store in cache ──
        self._read_cache.store(normalized, offset=1, limit=MAX_READ_LINES, content=output)

        return ToolResult(success=True, output=output)

    def reset_read_cache(self) -> None:
        """Reset the read cache. Called at the start of each subagent run."""
        self._read_cache.reset()

    def clone_with_fresh_cache(self) -> "FileReadTool":
        """Return a new FileReadTool with a fresh cache for subagent isolation."""
        return FileReadTool(read_cache=FileReadCache())


class FileViewTool(BaseTool):
    """
    分窗口查看文件，每次返回 VIEW_WINDOW_LINES 行。

    Uses FileReadCache to prevent repeated reads of the same range.
    Shares the same per-file frequency cap with file_read.

    params:
        path (str):       文件路径
        start_line (int): 从第几行开始（1-indexed，默认 1）
    """

    def __init__(self, read_cache: FileReadCache | None = None) -> None:
        self._read_cache = read_cache or FileReadCache()

    @property
    def name(self) -> str:
        return "file_view"

    @property
    def description(self) -> str:
        return (
            f"View a specific section of a file, {VIEW_WINDOW_LINES} lines at a time. "
            f"Use start_line to scroll through large files. Lines are 1-indexed."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file",
                },
                "start_line": {
                    "type": "integer",
                    "description": "First line to show (1-indexed, default 1)",
                },
            },
            "required": ["path"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        path = Path(params.get("path", ""))
        start_line = max(1, int(params.get("start_line", 1)))
        normalized = str(path.resolve())

        # ── Cache check ──
        limit = VIEW_WINDOW_LINES
        cached = self._read_cache.check(normalized, offset=start_line, limit=limit)
        if cached is not None:
            count = self._read_cache.read_counts.get(normalized, 0) + 1
            self._read_cache.read_counts[normalized] = count
            logger.debug("file_view cache hit: %s#%d (read #%d)", normalized, start_line, count)
            return ToolResult(
                success=True,
                output=(
                    f"{cached}\n\n"
                    f"[CACHED] Lines {start_line}-{start_line + limit - 1} of this file "
                    f"were already read in this run. "
                    f"(read #{count}/{MAX_READS_PER_FILE}). "
                    "Use the earlier observation instead of re-reading."
                ),
            )

        # ── Frequency cap ──
        count = self._read_cache.count_and_check(normalized)
        if count < 0:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"File '{path}' has already been read {MAX_READS_PER_FILE} times "
                    "in this run. Use the content you already have to complete your task."
                ),
            )

        # ── Actual read ──
        if not path.exists():
            return ToolResult(success=False, output="", error=f"File not found: {path}")
        if not path.is_file():
            return ToolResult(success=False, output="", error=f"Not a file: {path}")

        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            return ToolResult(success=False, output="", error=str(e))

        total = len(lines)
        if start_line > total:
            return ToolResult(
                success=False,
                output="",
                error=f"start_line {start_line} exceeds file length ({total} lines)",
            )

        end_line = min(start_line + VIEW_WINDOW_LINES - 1, total)
        window = lines[start_line - 1 : end_line]

        numbered = "\n".join(
            f"{start_line + i:4d} | {line}"
            for i, line in enumerate(window)
        )

        nav = ""
        if end_line < total:
            nav = f"\n[Lines {start_line}–{end_line} of {total}. Next: file_view path={path} start_line={end_line + 1}]"
        else:
            nav = f"\n[Lines {start_line}–{end_line} of {total}. End of file.]"

        output = numbered + nav

        # ── Store in cache ──
        self._read_cache.store(normalized, offset=start_line, limit=limit, content=output)

        return ToolResult(success=True, output=output)

    def reset_read_cache(self) -> None:
        """Reset the read cache. Called at the start of each subagent run."""
        self._read_cache.reset()

    def clone_with_fresh_cache(self) -> "FileViewTool":
        """Return a new FileViewTool with a fresh cache for subagent isolation."""
        return FileViewTool(read_cache=FileReadCache())


class FileWriteTool(BaseTool):
    """
    写入文件（全量覆盖）。自动创建父目录。

    params:
        path (str):    文件路径
        content (str): 要写入的内容
    """

    def __init__(self, allowed_paths: list[str | Path] | None = None) -> None:
        self._allowed_paths = (
            {Path(path).expanduser().resolve() for path in allowed_paths}
            if allowed_paths is not None
            else None
        )

    @property
    def name(self) -> str:
        return "file_write"

    @property
    def risk_level(self) -> str:
        from tools.base import RiskLevel
        return RiskLevel.MEDIUM

    @property
    def description(self) -> str:
        return (
            "Write content to a file, replacing its entire contents. "
            "Parent directories are created automatically. "
            "Always read the file first before writing to avoid losing existing content."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write",
                },
                "content": {
                    "type": "string",
                    "description": "Full content to write to the file",
                },
            },
            "required": ["path", "content"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        path = Path(params.get("path", ""))
        content = params.get("content", "")
        target_path = path.expanduser().resolve()

        if self._allowed_paths is not None and target_path not in self._allowed_paths:
            return ToolResult(
                success=False,
                output="",
                error=f"Permission denied: Writing to {target_path} is not allowed.",
            )

        # 覆盖大文件时发出警告
        warning = ""
        if target_path.exists() and target_path.is_file():
            try:
                existing_lines = target_path.read_text(encoding="utf-8", errors="replace").count("\n")
                if existing_lines > 50:
                    warning = (
                        f"\n⚠️  WARNING: Overwrote existing file with {existing_lines}+ lines. "
                        "For targeted edits, use file_edit instead of file_write to avoid data loss."
                    )
            except OSError:
                pass

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content, encoding="utf-8")
        except OSError as e:
            return ToolResult(success=False, output="", error=str(e))

        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return ToolResult(
            success=True,
            output=f"Written {line_count} lines to {path}{warning}",
        )