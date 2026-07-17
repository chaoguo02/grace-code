"""
tools/search_tool.py

Code search tools aligned with Claude Code:
- Grep:  search file contents with regex (CC-aligned parameters)
- Glob:  find files by name pattern
- find_symbol: locate function/class definitions in Python

Design:
- Python native implementation (no external ripgrep dependency)
- find_symbol uses regex for def/class matching
- Results capped to prevent context explosion
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.base import (
    BaseTool,
    PathAccess,
    ToolEffect,
    ToolMetadata,
    ToolResult,
    is_path_safe,
    sanitize_path,
)


MAX_RESULTS = 50
MAX_LINE_LENGTH = 200

# Directories skipped during recursive search
# Exact-match skip dirs (fast path)
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", "dist", "build",
})

# Prefix-match skip dirs (for temp/artifact dirs with hash suffixes)
_SKIP_DIR_PREFIXES: tuple[str, ...] = (
    ".pytest-", ".tmp-", ".pytest_tmp", ".tmp",
    ".pytest-of-", ".pytest-plan-",
)


def _resolve_search_path(raw_path: object, workspace_root: str) -> tuple[Path | None, str]:
    try:
        path = Path(sanitize_path(str(raw_path or "."), workspace_root))
    except ValueError as exc:
        return None, str(exc)
    if not is_path_safe(str(path), workspace_root):
        return None, f"Path outside workspace: {path}"
    return path, ""


# ---------------------------------------------------------------------------
# Grep — CC-aligned search_text
# ---------------------------------------------------------------------------

class SearchTextTool(BaseTool):
    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.DISCOVER_WORKSPACE}),
        path_access=PathAccess.DISCOVER,
        path_parameter="path",
    )
    """Search file contents with regex (CC-aligned Grep tool).

    CC-aligned parameters:
        pattern (str):     regex pattern (ripgrep syntax, not POSIX)
        path (str):        file or directory to search (default: cwd)
        glob (str):        glob filter for files (e.g. '**/*.tsx')
        output_mode (str): 'files_with_matches' (default), 'content', or 'count'
        -i (bool):         case-insensitive search
        -A (int):          lines of context after each match
        -B (int):          lines of context before each match
        -C (int):          lines of context before+after
        head_limit (int):  max results to return (default: 50)
        multiline (bool):  match across line boundaries
        type (str):        ripgrep file type filter (e.g. 'py', 'rust')
    """

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        self._workspace_root = str(Path(workspace_root or Path.cwd()).resolve())

    aliases = ("search_text",)

    @property
    def name(self) -> str:
        return "Grep"

    @property
    def description(self) -> str:
        return (
            "Search file contents with regex (ripgrep syntax). "
            "Default output_mode: files_with_matches (file paths only). "
            "Use 'content' for matching lines or 'count' for per-file counts. "
            "Scope with glob (e.g. '**/*.py') or type (e.g. 'py'). "
            f"Results capped at {MAX_RESULTS} by default."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for. Escape metacharacters: interface\\{\\} in Go.",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in (default: current workspace directory)",
                },
                "glob": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g. '**/*.tsx', '*.py'). Default: all text files",
                },
                "output_mode": {
                    "type": "string",
                    "description": "Output mode: 'files_with_matches' (default, paths only), 'content' (lines with file:line:), 'count' (per-file counts)",
                },
                "-i": {
                    "type": "boolean",
                    "description": "Case-insensitive search (default: false / case-sensitive)",
                },
                "-A": {
                    "type": "integer",
                    "description": "Lines of context to show after each match",
                },
                "-B": {
                    "type": "integer",
                    "description": "Lines of context to show before each match",
                },
                "-C": {
                    "type": "integer",
                    "description": "Lines of context to show before and after each match",
                },
                "head_limit": {
                    "type": "integer",
                    "description": f"Maximum results to return. Default: {MAX_RESULTS}",
                },
                "multiline": {
                    "type": "boolean",
                    "description": "Match across line boundaries (regex . matches newlines). Default: false",
                },
                "type": {
                    "type": "string",
                    "description": "Ripgrep file type filter (e.g. 'py', 'js', 'rust', 'go')",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "DEPRECATED. Use 'glob' instead.",
                    "deprecated": True,
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "DEPRECATED. Use '-i' instead (inverted).",
                    "deprecated": True,
                },
            },
            "required": ["pattern"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        raw_pattern = params.get("pattern", "")
        search_path, path_error = _resolve_search_path(
            params.get("path", "."), self._workspace_root,
        )
        if search_path is None:
            return ToolResult(success=False, output="", error=path_error)

        # CC-aligned params with backward-compatible fallbacks
        file_glob = params.get("glob") or params.get("file_pattern", "*")
        case_insensitive = params.get("-i", False) or not params.get("case_sensitive", True)
        output_mode = params.get("output_mode", "files_with_matches")
        head_limit = int(params.get("head_limit", MAX_RESULTS))
        multiline = params.get("multiline", False)
        context_after = int(params.get("-A", 0))
        context_before = int(params.get("-B", 0))
        context_both = int(params.get("-C", 0))
        if context_both > 0:
            context_after = context_both
            context_before = context_both
        file_type = params.get("type")
        if file_type:
            file_glob = f"*.{file_type}"

        flags = re.IGNORECASE if case_insensitive else 0
        if multiline:
            flags |= re.DOTALL
        try:
            regex = re.compile(raw_pattern, flags)
        except re.error as e:
            return ToolResult(success=False, output="", error=f"Invalid regex: {e}")

        if not search_path.exists():
            return ToolResult(success=False, output="", error=f"Path not found: {search_path}")

        # Collect matches with optional context
        matches: list[str] = []
        match_counts: dict[str, int] = {}
        files = _iter_files(search_path, file_glob)

        import time as _time
        _search_deadline = _time.monotonic() + 15.0  # 15s hard timeout

        for filepath in files:
            if len(matches) >= head_limit:
                break
            if _time.monotonic() > _search_deadline:
                matches.append("[Search timed out after 15s — partial results below]")
                break
            try:
                file_lines = filepath.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            rel_path = str(filepath)
            file_match_count = 0

            for lineno, line in enumerate(file_lines, start=1):
                if regex.search(line):
                    file_match_count += 1
                    if output_mode == "count":
                        continue

                    # Build match line with optional context
                    start_ctx = max(0, lineno - context_before - 1)
                    end_ctx = min(len(file_lines), lineno + context_after)
                    if context_after or context_before:
                        ctx_lines = file_lines[start_ctx:end_ctx]
                        for ci, cline in enumerate(ctx_lines):
                            cline_num = start_ctx + ci + 1
                            marker = ":" if cline_num == lineno else "-"
                            display = cline[:MAX_LINE_LENGTH]
                            if len(cline) > MAX_LINE_LENGTH:
                                display += " ..."
                            matches.append(f"{rel_path}:{cline_num}:{marker} {display}")
                    else:
                        display_line = line[:MAX_LINE_LENGTH]
                        if len(line) > MAX_LINE_LENGTH:
                            display_line += " ..."
                        matches.append(f"{rel_path}:{lineno}: {display_line}")

                    if output_mode != "count" and len(matches) >= head_limit:
                        break

            if file_match_count > 0:
                match_counts[rel_path] = file_match_count

        # Build output by mode
        if output_mode == "count":
            if not match_counts:
                return ToolResult(success=True, output=f"No matches found for '{raw_pattern}'")
            total = sum(match_counts.values())
            lines_out = [f"{p}: {c}" for p, c in sorted(match_counts.items())]
            lines_out.append(f"\n[Total: {total} matches across {len(match_counts)} files]")
            return ToolResult(success=True, output="\n".join(lines_out))

        if output_mode == "files_with_matches":
            if not match_counts:
                return ToolResult(success=True, output=f"No matches found for '{raw_pattern}'")
            unique_files = sorted(match_counts.keys())
            output = "\n".join(unique_files)
            if len(unique_files) >= head_limit:
                output += f"\n[Showing first {head_limit} matching files, there may be more]"
            return ToolResult(success=True, output=output)

        # content mode (default)
        if not matches:
            return ToolResult(success=True, output=f"No matches found for '{raw_pattern}'")

        suffix = f"\n[Showing {min(len(matches), head_limit)} matches]"
        if len(matches) >= head_limit:
            suffix = f"\n[Showing first {head_limit} matches, there may be more]"

        return ToolResult(success=True, output="\n".join(matches[:head_limit]) + suffix)


# ---------------------------------------------------------------------------
# Glob — CC-aligned find_files
# ---------------------------------------------------------------------------

class FindFilesTool(BaseTool):
    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.DISCOVER_WORKSPACE}),
        path_access=PathAccess.DISCOVER,
        path_parameter="path",
    )
    """Find files by glob pattern (CC-aligned Glob tool).

    params:
        pattern (str): glob pattern (e.g. "*.py", "**/*.tsx")
        path (str):    root directory to search (default: cwd)
    """

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        self._workspace_root = str(Path(workspace_root or Path.cwd()).resolve())

    aliases = ("find_files",)

    @property
    def name(self) -> str:
        return "Glob"

    @property
    def description(self) -> str:
        return (
            "Find files by name pattern (glob style). "
            "Supports ** for recursive matching. "
            "Example: pattern='**/*.ts' finds all TypeScript files. "
            f"Returns at most {MAX_RESULTS} results."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern for file names (e.g. '*.py', '**/*.tsx', '*.{json,yaml}')",
                },
                "path": {
                    "type": "string",
                    "description": "Root directory to search in (default: current directory)",
                },
            },
            "required": ["pattern"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        pattern = params.get("pattern", "")
        search_path, path_error = _resolve_search_path(
            params.get("path", "."), self._workspace_root,
        )
        if search_path is None:
            return ToolResult(success=False, output="", error=path_error)

        if not search_path.exists():
            return ToolResult(success=False, output="", error=f"Path not found: {search_path}")

        results: list[str] = []
        for filepath in _iter_files(search_path, pattern):
            results.append(str(filepath))
            if len(results) >= MAX_RESULTS:
                break

        if not results:
            return ToolResult(
                success=True,
                output=f"No files found matching '{pattern}' in {search_path}",
            )

        suffix = ""
        if len(results) == MAX_RESULTS:
            suffix = f"\n[Showing first {MAX_RESULTS} results]"

        return ToolResult(success=True, output="\n".join(results) + suffix)


# ---------------------------------------------------------------------------
# find_symbol
# ---------------------------------------------------------------------------

class FindSymbolTool(BaseTool):
    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.DISCOVER_WORKSPACE}),
        path_access=PathAccess.DISCOVER,
        path_parameter="path",
    )
    """Find function/class definitions in Python files using regex.

    params:
        symbol (str): function or class name (partial match supported)
        path (str):   root directory to search (default: cwd)
    """

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        self._workspace_root = str(Path(workspace_root or Path.cwd()).resolve())

    @property
    def name(self) -> str:
        return "find_symbol"

    @property
    def description(self) -> str:
        return (
            "Find function or class definitions in Python files. "
            "Searches for 'def symbol' or 'class symbol' patterns. "
            "Supports partial name matching."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Function or class name to find (partial match supported)",
                },
                "path": {
                    "type": "string",
                    "description": "Root directory to search in (default: current directory)",
                },
            },
            "required": ["symbol"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        symbol = params.get("symbol", "")
        search_path, path_error = _resolve_search_path(
            params.get("path", "."), self._workspace_root,
        )
        if search_path is None:
            return ToolResult(success=False, output="", error=path_error)

        if not symbol:
            return ToolResult(success=False, output="", error="symbol is required")

        pattern = re.compile(
            rf"^(\s*)(def|class)\s+({re.escape(symbol)}\w*)\s*[:(]",
            re.MULTILINE,
        )

        matches: list[str] = []
        for filepath in _iter_files(search_path, "*.py"):
            if len(matches) >= MAX_RESULTS:
                break
            try:
                content = filepath.read_text(encoding="utf-8", errors="replace")
                for m in pattern.finditer(content):
                    lineno = content[: m.start()].count("\n") + 1
                    kind = m.group(2)
                    name = m.group(3)
                    indent = len(m.group(1))
                    scope = "method" if indent > 0 else "top-level"
                    matches.append(f"{filepath}:{lineno}: {kind} {name} ({scope})")
                    if len(matches) >= MAX_RESULTS:
                        break
            except OSError:
                continue

        if not matches:
            return ToolResult(success=True, output=f"No definition found for '{symbol}'")

        return ToolResult(success=True, output="\n".join(matches))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _iter_files(root: Path, glob_pattern: str):
    """Recursively iterate files matching glob, skipping _SKIP_DIRS."""
    if root.is_file():
        yield root
        return

    for filepath in sorted(root.rglob(glob_pattern)):
        skip = False
        for part in filepath.parts:
            if part in _SKIP_DIRS:
                skip = True
                break
            if part.startswith(_SKIP_DIR_PREFIXES):
                skip = True
                break
        if skip:
            continue
        if filepath.is_file():
            yield filepath
