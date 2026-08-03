"""Phase 1A: 乐观写入 + 即时回滚 — Before/Target 测试。

对齐 Claude Code "不污染工作区"原则：写入要么成功且语法有效，
要么回滚且返回 ToolFailure（含具体语法错误，让 LLM 自我纠正）。

Before（实现前）：本文件测试全部 FAIL —— 证明"Edit/Write 破坏语法后
直接写盘、不校验不回滚"的缺口存在。
Target（实现后）：全部 PASS。
"""

from __future__ import annotations

import json

import pytest

from tools.file_edit_tool import FileEditTool
from tools.file_tool import FileReadCache, FileWriteTool


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_valid_py(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def foo():\n    return 1\n", encoding="utf-8")


def _make_valid_json(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"key": "value"}, indent=2) + "\n", encoding="utf-8")


def _edit_tool(tmp_path):
    cache = FileReadCache()
    tool = FileEditTool(read_cache=cache, workspace_root=str(tmp_path))
    return tool, cache


def _write_tool(tmp_path):
    cache = FileReadCache()
    tool = FileWriteTool(read_cache=cache, workspace_root=str(tmp_path))
    return tool, cache


def _satisfy_read_before_edit(cache, path) -> None:
    """Store file content into the shared read cache (Read-before-Edit gate)."""
    cache.store(str(path.resolve()), offset=None, limit=None, content=path.read_text())


# ── Edit 破坏 Python 语法 ─────────────────────────────────────────────────

def test_edit_invalid_python_syntax_rolls_back(tmp_path):
    """Edit 破坏 Python 缩进 → 必须回滚且返回失败。"""
    f = tmp_path / "mod.py"
    _make_valid_py(f)
    tool, cache = _edit_tool(tmp_path)
    _satisfy_read_before_edit(cache, f)

    result = tool.execute({
        "path": "mod.py",
        "old_str": "    return 1",
        "new_str": "return 1",    # 无缩进 → IndentationError
    })

    assert result.success is False, (
        f"Edit 破坏语法必须返回失败，got success=True error={result.error!r}"
    )
    assert "syntax error" in (result.error or "").lower(), (
        f"错误信息应包含语法错误详情，got {result.error!r}"
    )
    assert f.read_text() == "def foo():\n    return 1\n", (
        "文件必须恢复到 Edit 前的内容（不得污染工作区）"
    )


def test_edit_valid_content_passes(tmp_path):
    """合法 Edit 正常写入成功。"""
    f = tmp_path / "mod.py"
    _make_valid_py(f)
    tool, cache = _edit_tool(tmp_path)
    _satisfy_read_before_edit(cache, f)

    result = tool.execute({
        "path": "mod.py",
        "old_str": "    return 1",
        "new_str": "    return 42",
    })

    assert result.success is True, f"合法 Edit 应成功，got {result.error}"
    assert "return 42" in f.read_text()


# ── Write 破坏 JSON 语法 ──────────────────────────────────────────────────

def test_write_invalid_json_rolls_back(tmp_path):
    """Write 写出非法 JSON → 必须回滚且返回失败。"""
    f = tmp_path / "conf.json"
    _make_valid_json(f)
    tool, cache = _write_tool(tmp_path)
    # 满足 Read-before-Write 守卫，让测试真正针对 JSON 语法校验而非守卫拦截
    _satisfy_read_before_edit(cache, f)

    result = tool.execute({
        "path": "conf.json",
        "content": '{"key": "value",}',  # trailing comma → invalid JSON
    })

    assert result.success is False, (
        f"Write 非法 JSON 必须返回失败，got success={result.success} error={result.error!r}"
    )
    assert f.read_text() == json.dumps({"key": "value"}, indent=2) + "\n", (
        "文件必须恢复到 Write 前的内容"
    )


def test_write_valid_content_passes(tmp_path):
    """合法 Write 正常写入成功。"""
    f = tmp_path / "new.py"
    tool, _cache = _write_tool(tmp_path)

    result = tool.execute({
        "path": "new.py",
        "content": "x = 1\n",
    })

    assert result.success is True, f"合法 Write 应成功，got {result.error}"
    assert f.read_text() == "x = 1\n"


# ── 校验器自身失败也必须安全回滚 ────────────────────────────────────────

def test_rollback_preserves_original_bytes(tmp_path):
    """回滚后字节级一致（无 BOM 变化/无尾随空行）。"""
    f = tmp_path / "mod.py"
    original = b"def foo():\n    return 1\n"
    f.write_bytes(original)
    tool, cache = _edit_tool(tmp_path)
    _satisfy_read_before_edit(cache, f)

    tool.execute({
        "path": "mod.py",
        "old_str": "    return 1",
        "new_str": "return 1",
    })

    assert f.read_bytes() == original, "回滚必须字节级一致"


def test_no_tmp_residue_after_rollback(tmp_path):
    """回滚后不得残留 .tmp 文件。"""
    f = tmp_path / "mod.py"
    _make_valid_py(f)
    tool, cache = _edit_tool(tmp_path)
    _satisfy_read_before_edit(cache, f)

    tool.execute({
        "path": "mod.py",
        "old_str": "    return 1",
        "new_str": "return 1",
    })

    residue = [p for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert residue == [], f"回滚后不得残留临时文件，got {residue}"
