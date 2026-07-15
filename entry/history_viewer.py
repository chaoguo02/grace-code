"""
entry/history_viewer.py

对话历史可视化。将 EventLog JSONL 文件渲染为人类可读格式，
支持列出、查看、搜索历史会话。

存储位置: ~/.forge-agent/history/
每次 chat session 的日志自动归档到此目录。
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from entry.renderer import (
    _bold, _cyan, _dim, _green, _red, _yellow, _magenta,
    _highlight_diff, format_diagnostic,
)

# Read-only display tools whose full output is never useful in history view.
# Prefer effects-based decisions (tools/display.py) when registry is available;
# this set is a pragmatic fallback for serialized history data.
_READ_ONLY_DISPLAY_TOOLS = frozenset({
    "file_read", "file_view", "find_files", "find_symbol",
})


def _is_read_only_display(tool_name: str) -> bool:
    return tool_name in _READ_ONLY_DISPLAY_TOOLS


# ---------------------------------------------------------------------------
# 历史目录管理
# ---------------------------------------------------------------------------

def get_history_dir() -> Path:
    """获取历史记录目录，不存在则创建。"""
    history_dir = Path.home() / ".forge-agent" / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    return history_dir


def archive_log(log_path: str | Path) -> Path | None:
    """将 event log 文件归档到历史目录。返回归档后的路径。"""
    src = Path(log_path)
    if not src.exists():
        return None
    dest_dir = get_history_dir()
    dest = dest_dir / src.name
    if dest.exists():
        return dest
    shutil.copy2(src, dest)
    return dest


# ---------------------------------------------------------------------------
# 历史列表
# ---------------------------------------------------------------------------

def list_history(limit: int = 20) -> list[dict[str, Any]]:
    """列出最近的历史会话，返回摘要列表。"""
    history_dir = get_history_dir()
    files = sorted(
        history_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]

    results = []
    for f in files:
        summary = _extract_summary(f)
        if summary:
            results.append(summary)
    return results


def _extract_summary(log_path: Path) -> dict[str, Any] | None:
    """从 JSONL 文件提取会话摘要。"""
    try:
        first_event = None
        last_event = None
        action_count = 0
        task_desc = ""

        with open(log_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if first_event is None:
                    first_event = event
                last_event = event

                etype = event.get("event_type", "")
                if etype == "task_start":
                    task_desc = event.get("payload", {}).get(
                        "task", {}
                    ).get("description", "")[:80]
                elif etype == "action":
                    action_count += 1

        if first_event is None:
            return None

        timestamp = first_event.get("timestamp", "")
        status = "unknown"
        if last_event:
            lt = last_event.get("event_type", "")
            if lt == "task_complete":
                status = "success"
            elif lt == "task_failed":
                status = "failed"

        return {
            "file": log_path.name,
            "path": str(log_path),
            "timestamp": timestamp,
            "task": task_desc,
            "steps": action_count,
            "status": status,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 历史详情渲染
# ---------------------------------------------------------------------------

def render_history_detail(log_path: str | Path) -> str:
    """将单个 log 文件渲染为人类可读文本。"""
    path = Path(log_path)
    if not path.exists():
        return f"File not found: {path}"

    lines: list[str] = []
    lines.append(_bold(f"\n{'━' * 60}"))
    lines.append(_bold(f"  Session: {path.name}"))
    lines.append(f"{'━' * 60}")

    with open(path, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            rendered = _render_event(event)
            if rendered:
                lines.append(rendered)

    lines.append(_bold(f"{'━' * 60}\n"))
    return "\n".join(lines)


def _render_event(event: dict) -> str | None:
    """渲染单个事件为彩色文本。"""
    etype = event.get("event_type", "")
    payload = event.get("payload", {})
    ts = event.get("timestamp", "")[11:19]  # HH:MM:SS

    if etype == "task_start":
        task = payload.get("task", {})
        desc = task.get("description", "")[:100]
        return (
            f"\n{_dim(ts)} {_bold('TASK START')}\n"
            f"  {_cyan(desc)}\n"
            f"  repo: {task.get('repo_path', '?')}"
        )

    elif etype == "action":
        step = payload.get("step", 0)
        action = payload.get("action", {})
        atype = action.get("action_type", "")
        thought = action.get("thought", "")[:120]
        tcs = action.get("tool_calls") or []

        parts = [f"{_dim(ts)} {_yellow(f'[{step}]')} {atype}"]
        if thought:
            parts.append(f"  {_dim(thought)}")
        if tcs:
            for tc in tcs:
                name = tc.get("name", "")
                params = tc.get("params", {})
                key = ""
                for k in ("cmd", "path", "pattern", "symbol"):
                    if k in params:
                        key = f" → {str(params[k])[:50]}"
                        break
                parts.append(f"  {_cyan(name)}{key}")
        return "\n".join(parts)

    elif etype == "observation":
        obs = payload.get("observation", {})
        status = obs.get("status", "")
        output = obs.get("output", "")
        error = obs.get("error")

        if status == "success":
            preview = output.splitlines()[:5]
            text = _green("  ✓")
            if preview and not _is_read_only_display(obs.get("tool_name", "")):
                text += "\n" + "\n".join(
                    f"    {_dim(l)}" for l in preview
                )
            return f"{_dim(ts)} {text}"
        else:
            return f"{_dim(ts)} {_red(f'  ✗ {error or output[:100]}')}"

    elif etype == "reflection":
        reason = payload.get("reason", "")
        return f"{_dim(ts)} {_yellow(f'  ⟳ {reason}')}"

    elif etype == "task_complete":
        summary = payload.get("summary", "")[:100]
        return f"\n{_dim(ts)} {_green(_bold('✓ COMPLETE'))}\n  {summary}"

    elif etype == "task_failed":
        reason = payload.get("reason", "")[:100]
        return f"\n{_dim(ts)} {_red(_bold('✗ FAILED'))}\n  {reason}"

    return None


# ---------------------------------------------------------------------------
# 搜索历史
# ---------------------------------------------------------------------------

def search_history(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """在历史记录中搜索包含 query 的会话。"""
    query_lower = query.lower()
    history_dir = get_history_dir()
    results = []

    files = sorted(
        history_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    for f in files:
        if len(results) >= limit:
            break
        try:
            with open(f, "r", encoding="utf-8") as fh:
                content = fh.read()
            if query_lower in content.lower():
                summary = _extract_summary(f)
                if summary:
                    results.append(summary)
        except Exception:
            continue

    return results
