"""Phase 1B: 隐式路径黑名单 + readonly_paths — Target 测试。

对齐 Claude Code：.git/, .env, __pycache__/, node_modules/, *.lock 即使
在 workspace 内也默认拒绝写入；Read 不受限；显式 override 可放行。
"""

from __future__ import annotations

import pytest

from tools.file_edit_tool import FileEditTool
from tools.file_tool import FileReadTool, FileReadCache, FileWriteTool


# ── Helpers ────────────────────────────────────────────────────────────────

def _write_tool(tmp_path, **kwargs):
    kwargs.setdefault("read_cache", FileReadCache())
    kwargs.setdefault("workspace_root", str(tmp_path))
    return FileWriteTool(**kwargs)


def _edit_tool(tmp_path, **kwargs):
    cache = kwargs.setdefault("read_cache", FileReadCache())
    kwargs.setdefault("workspace_root", str(tmp_path))
    tool = FileEditTool(**kwargs)
    return tool, cache


def _satisfy_read(cache, path) -> None:
    cache.store(str(path.resolve()), offset=None, limit=None,
                content=path.read_text())


# ── 默认保护列表 ──────────────────────────────────────────────────────────

def test_write_to_git_dir_is_blocked(tmp_path):
    (tmp_path / ".git").mkdir()
    tool = _write_tool(tmp_path)
    result = tool.execute({"path": ".git/config", "content": "[core]\n"})
    assert result.success is False
    assert "protected" in (result.error or "").lower()


def test_write_to_env_file_is_blocked(tmp_path):
    (tmp_path / ".env").write_text("SECRET=x\n", encoding="utf-8")
    tool = _write_tool(tmp_path)
    result = tool.execute({"path": ".env", "content": "SECRET=y\n"})
    assert result.success is False
    assert "protected" in (result.error or "").lower()


def test_write_to_pycache_is_blocked(tmp_path):
    (tmp_path / "__pycache__").mkdir()
    tool = _write_tool(tmp_path)
    result = tool.execute({"path": "__pycache__/mod.pyc", "content": "x"})
    assert result.success is False


def test_write_to_node_modules_is_blocked(tmp_path):
    (tmp_path / "node_modules").mkdir()
    tool = _write_tool(tmp_path)
    result = tool.execute({"path": "node_modules/pkg/index.js", "content": "x"})
    assert result.success is False


def test_write_to_lockfile_is_blocked(tmp_path):
    (tmp_path / "requirements.lock").write_text("a==1\n", encoding="utf-8")
    tool = _write_tool(tmp_path)
    result = tool.execute({"path": "requirements.lock", "content": "b==2\n"})
    assert result.success is False
    assert "protected" in (result.error or "").lower()


def test_edit_to_node_modules_is_blocked(tmp_path):
    p = tmp_path / "node_modules" / "pkg" / "index.js"
    p.parent.mkdir(parents=True)
    p.write_text("export const x = 1;\n", encoding="utf-8")
    tool, cache = _edit_tool(tmp_path)
    _satisfy_read(cache, p)

    result = tool.execute({
        "path": "node_modules/pkg/index.js",
        "old_str": "export const x = 1;",
        "new_str": "export const x = 2;",
    })
    assert result.success is False
    assert "protected" in (result.error or "").lower()
    # 内容未被修改
    assert p.read_text() == "export const x = 1;\n"


# ── 自定义 readonly_paths ─────────────────────────────────────────────────

def test_readonly_paths_config_effective(tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "key.txt").write_text("k\n", encoding="utf-8")
    tool = _write_tool(tmp_path, protected_paths=("secrets/*",))

    result = tool.execute({"path": "secrets/key.txt", "content": "new\n"})
    assert result.success is False
    assert "protected" in (result.error or "").lower()


# ── Read 不受限 ────────────────────────────────────────────────────────────

def test_read_protected_path_allowed(tmp_path):
    (tmp_path / ".env").write_text("SECRET=x\n", encoding="utf-8")
    tool = FileReadTool(workspace_root=str(tmp_path))

    result = tool.execute({"path": ".env"})
    assert result.success is True
    assert "SECRET=x" in result.output


# ── 显式 override 放行 ─────────────────────────────────────────────────────

def test_override_allows_write_to_protected(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SECRET=x\n", encoding="utf-8")
    cache = FileReadCache()
    cache.store(str(env.resolve()), offset=None, limit=None, content=env.read_text())
    tool = FileWriteTool(
        read_cache=cache, workspace_root=str(tmp_path),
        allow_overrides=(".env",),
    )

    result = tool.execute({"path": ".env", "content": "SECRET=override\n"})
    assert result.success is True, f"override 应放行受保护路径，got {result.error}"
    assert env.read_text() == "SECRET=override\n"
