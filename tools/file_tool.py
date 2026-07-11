"""
tools/file_tool.py

文件操作工具，提供四个 action：
- file_read:   读取文件全部内容
- file_view:   分窗口查看文件（防止一次读爆上下文）
- file_write:  写入文件（全量覆盖）
- file_edit:   精确字符串替换编辑

设计原则：
- file_read 对大文件做行数截断，超出时提示用 file_view 分页
- file_view 维护"窗口"概念，每次返回固定行数，agent 可 scroll
- file_write 写入前自动创建父目录，写入后返回行数确认
- 所有路径都限制在 repo_path 内（防止读取系统文件）
- FileReadCache: Session-global, mtime-verified cache shared across parent
  and all subagents. Content-hash semantics via filesystem mtime — if the
  file hasn't been modified since the last read, return cached content.
  Write tools invalidate cache entries, guaranteeing freshness.
"""

from __future__ import annotations

import logging
import os as _os
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


# ═══════════════════════════════════════════════════════════════════════════
# FileReadCache — Session-global, mtime-verified, cross-agent shared
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class _CacheEntry:
    """A single read range in the cache, verified by filesystem mtime."""

    offset: int | None   # 1-indexed start line (None = full file)
    limit: int | None    # number of lines read (None = full file)
    end_line: int | None  # computed: offset + limit - 1 (None = full file)
    content: str
    mtime_ns: int = 0    # st_mtime_ns at read time — mismatch = cache invalid


@dataclass
class FileReadCache:
    """Session-global cache of file read results with mtime verification.

    Shared across parent agent and ALL subagents. Unlike the previous
    per-agent isolation design, this uses filesystem mtime to guarantee
    freshness: if a file hasn't been modified since it was cached, there
    is zero benefit to re-reading it from disk — the content is identical.

    Write tools (file_write, file_edit) call invalidate() to purge cached
    entries for modified paths, so subsequent reads always get fresh content.

    Key: normalized absolute path
    Value: list of cached ranges, each with stored mtime
    """

    entries: dict[str, list[_CacheEntry]] = field(default_factory=dict)
    _hit_count: int = 0   # diagnostics: total cache hits this session

    # ── Public API ──

    def check(self, normalized_path: str, offset: int | None, limit: int | None) -> str | None:
        """Return cached content if mtime matches and range is fully covered.

        Performs a stat() call to verify the file hasn't been modified since
        the cache entry was stored. O(1) filesystem check, not O(n) hashing.
        """
        entries = self.entries.get(normalized_path)
        if not entries:
            return None

        # Verify mtime hasn't changed — if any entry is stale, invalidate all
        try:
            current_mtime = os.stat(normalized_path).st_mtime_ns
        except OSError:
            # File doesn't exist or can't be stat'd. If the cached entry was
            # also stored without mtime (mtime_ns=0), return it (test/offline use).
            # Otherwise the file was deleted since caching → miss.
            if entries[0].mtime_ns == 0:
                current_mtime = 0  # fall through to range check
            else:
                return None

        first_entry = entries[0]
        if first_entry.mtime_ns != 0 and current_mtime != 0 and current_mtime != first_entry.mtime_ns:
            # File was modified since cache — purge and miss
            del self.entries[normalized_path]
            logger.debug("FileReadCache invalidated (mtime changed): %s", normalized_path)
            return None

        # Check range coverage
        requested_start = offset if offset is not None else 1
        requested_end: int | None
        if offset is None and limit is None:
            requested_end = None
        elif limit is not None:
            requested_end = requested_start + limit - 1
        else:
            requested_end = None

        for entry in entries:
            if requested_end is None:
                if entry.end_line is None:
                    self._hit_count += 1
                    return entry.content
                continue
            if entry.end_line is None:
                self._hit_count += 1
                return entry.content
            if entry.offset is not None and entry.end_line is not None:
                if entry.offset <= requested_start and entry.end_line >= requested_end:
                    self._hit_count += 1
                    return entry.content

        return None

    def store(
        self,
        normalized_path: str,
        offset: int | None,
        limit: int | None,
        content: str,
    ) -> None:
        """Record a read operation in the cache with current mtime."""
        end_line: int | None
        if offset is None and limit is None:
            end_line = None
        elif limit is not None:
            start = offset if offset is not None else 1
            end_line = start + limit - 1
        else:
            end_line = None

        try:
            mtime_ns = os.stat(normalized_path).st_mtime_ns
        except OSError:
            mtime_ns = 0

        entry = _CacheEntry(
            offset=offset,
            limit=limit,
            end_line=end_line,
            content=content,
            mtime_ns=mtime_ns,
        )
        self.entries.setdefault(normalized_path, []).append(entry)

    def invalidate(self, normalized_path: str) -> None:
        """Remove all cached entries for a path. Called after file writes."""
        if normalized_path in self.entries:
            del self.entries[normalized_path]
            logger.debug("FileReadCache invalidated (write): %s", normalized_path)

    @property
    def hit_count(self) -> int:
        """Total cache hits this session (for diagnostics)."""
        return self._hit_count


class FileReadTool(BaseTool):
    is_read_only = True
    """
    读取文件内容。超过 MAX_READ_LINES 行时截断并提示。

    Uses a session-global FileReadCache with mtime verification.
    Cache is shared across parent and all subagents — no per-agent isolation.

    params:
        path (str): 文件路径（相对或绝对）
    """

    is_read_only = True

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

        # ── Cache check (mtime-verified, session-global) ──
        cached = self._read_cache.check(normalized, offset=1, limit=MAX_READ_LINES)
        if cached is not None:
            logger.debug("file_read cache hit: %s", normalized)
            return ToolResult(
                success=True,
                cached=True,
                output=(
                    f"{cached}\n\n"
                    f"[CACHED] File unchanged since last read — using cached content."
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


class FileViewTool(BaseTool):
    is_read_only = True
    """
    分窗口查看文件，每次返回 VIEW_WINDOW_LINES 行。

    Uses a session-global FileReadCache with mtime verification.
    Cache is shared across parent and all subagents — no per-agent isolation.

    params:
        path (str):       文件路径
        start_line (int): 从第几行开始（1-indexed，默认 1）
    """

    is_read_only = True

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

        # ── Cache check (mtime-verified, session-global) ──
        limit = VIEW_WINDOW_LINES
        cached = self._read_cache.check(normalized, offset=start_line, limit=limit)
        if cached is not None:
            logger.debug("file_view cache hit: %s#%d", normalized, start_line)
            return ToolResult(
                success=True,
                cached=True,
                output=(
                    f"{cached}\n\n"
                    f"[CACHED] Lines {start_line}-{start_line + limit - 1} unchanged "
                    f"since last read — using cached content."
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


class FileWriteTool(BaseTool):
    """
    写入文件（全量覆盖）。自动创建父目录。

    params:
        path (str):    文件路径
        content (str): 要写入的内容
    """

    def __init__(self, allowed_paths: list[str | Path] | None = None,
                 read_cache: FileReadCache | None = None,
                 workspace_root: str | None = None) -> None:
        self._allowed_paths = (
            {Path(path).expanduser().resolve() for path in allowed_paths}
            if allowed_paths is not None
            else None
        )
        self._read_cache = read_cache
        self._workspace_root = workspace_root

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
        ws = self._workspace_root

        # ── Layer 1: Sanitize path (remove ../ traversal at string level) ──
        if ws is not None:
            from tools.base import sanitize_path
            try:
                clean = sanitize_path(str(path), ws)
            except ValueError as e:
                return ToolResult(success=False, output="", error=str(e))
            path = Path(clean)

        # ── Layers 2+3: resolve parent + O_NOFOLLOW (TOCTOU protection) ──
        if ws is not None:
            from tools.base import resolve_safe_parent
            safe_path, err = resolve_safe_parent(str(path), ws)
            if err:
                return ToolResult(success=False, output="", error=err)

            from tools.base import safe_open_for_write
            fd, err = safe_open_for_write(safe_path)
            if err:
                return ToolResult(success=False, output="", error=err)
            _os.write(fd, content.encode("utf-8"))
            _os.close(fd)
            target_path = Path(safe_path)
        else:
            # No workspace — fall back to legacy behavior
            target_path = path.expanduser().resolve()
            if self._allowed_paths is not None and target_path not in self._allowed_paths:
                return ToolResult(
                    success=False, output="",
                    error=f"Permission denied: Writing to {target_path} is not allowed.",
                )
            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(content, encoding="utf-8")
            except OSError as e:
                return ToolResult(success=False, output="", error=str(e))

        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)

        # ── Invalidate read cache for this path ──
        if self._read_cache is not None:
            from tools.base import sanitize_path
            self._read_cache.invalidate(str(target_path))

        return ToolResult(
            success=True,
            output=f"Written {line_count} lines to {path}",
        )