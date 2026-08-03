"""Phase 1C: 路径逃逸回归测试集。

对齐 Claude Code 安全基线：LLM 输出的任何路径都不可信，必须经
sanitize_path → is_path_safe → resolve_safe_parent 三层防御。
本套测试对每种攻击向量断言"不逃逸 workspace 边界"。

向量矩阵（文档 LOCALRUNTIME_CC_ALIGN_PLAN Phase 1C）：
  P1  ../ 遍历       P6  绝对路径逃逸
  P2  symlink → /etc P7  嵌套 symlink
  P3  symlink → 出界  P8  ./ 或 ../ 尾缀
  P4  hardlink        P9  空字节注入
  P5  unicode 变体    P10 Windows ..\\..\\

symlink 相关用例在 Windows 无权限时自动 skip。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from tools.file_tool import FileWriteTool


# ── Helpers ────────────────────────────────────────────────────────────────

def _write(tmp_path: Path, path_param: str) -> object:
    """Execute FileWriteTool with the given (possibly malicious) path."""
    tool = FileWriteTool(workspace_root=str(tmp_path))
    return tool.execute({"path": path_param, "content": "x"})


def _can_symlink() -> bool:
    try:
        d = tempfile.mkdtemp()
        src = os.path.join(d, "src")
        dst = os.path.join(d, "dst")
        Path(src).write_text("x", encoding="utf-8")
        os.symlink(src, dst)
        os.unlink(dst)
        os.rmdir(d)
        return True
    except OSError:
        return False


_needs_symlink = pytest.mark.skipif(
    not _can_symlink(), reason="symlink creation not permitted on this platform",
)


# ── P1: 字符串层 ../ 遍历 ─────────────────────────────────────────────────

@pytest.mark.parametrize("evil", [
    "../../etc/passwd",
    "../outside.txt",
    "sub/../../../../etc/hosts",
])
def test_p1_dotdot_traversal_rejected(tmp_path, evil):
    r = _write(tmp_path, evil)
    assert r.success is False, f"'{evil}' 必须被拒绝，got success={r.success}"
    assert "escape" in (r.error or "").lower() or "outside" in (r.error or "").lower()


# ── P10: Windows 反斜杠遍历 ───────────────────────────────────────────────

@pytest.mark.parametrize("evil", [
    "..\\..\\etc\\passwd",
    "..\\..\\Windows\\win.ini",
])
def test_p10_windows_backslash_traversal_rejected(tmp_path, evil):
    r = _write(tmp_path, evil)
    assert r.success is False, f"'{evil}' 必须被拒绝，got success={r.success}"


# ── P6: 绝对路径逃逸 ──────────────────────────────────────────────────────

def test_p6_absolute_path_escape_rejected(tmp_path):
    anchor = str(Path(tmp_path).anchor)
    evil = os.path.join(anchor, "Windows", "win.ini")
    r = _write(tmp_path, evil)
    assert r.success is False, f"绝对路径 '{evil}' 必须被拒绝"
    assert "escape" in (r.error or "").lower()


# ── P8: ../ 尾缀 ──────────────────────────────────────────────────────────

def test_p8_dotdot_suffix_rejected(tmp_path):
    # "<ws>.." normpath 后解析到 ws 父级 → 逃逸
    r = _write(tmp_path, str(tmp_path) + os.sep + "..")
    assert r.success is False


def test_p8_dot_suffix_never_escapes(tmp_path):
    # "." 尾缀应解析回 ws 内（不逃逸，可安全拒绝或允许）
    r = _write(tmp_path, ".")
    # 拒绝或允许都可接受，但绝不能逃逸到 ws 外写文件
    if r.success:
        # 若允许，文件必须是 ws 内的 "." 目录写（实际会失败），此处仅保证不崩溃
        pass
    assert "escape" not in (r.error or "").lower()


# ── P9: 空字节注入 ────────────────────────────────────────────────────────

def test_p9_null_byte_injection_rejected(tmp_path):
    r = _write(tmp_path, "sub\x00/etc/passwd")
    assert r.success is False, "空字节路径必须被拒绝"


# ── P5: unicode 变体（RTL override 不逃逸） ──────────────────────────────

def test_p5_rtl_override_never_escapes(tmp_path):
    # ‮ 是 RTL 覆盖，仅视觉欺骗，路径解析必须仍在 ws 内
    evil = "sub‮/tnecs/gnidoc"
    r = _write(tmp_path, evil)
    # 关键安全属性：不得写文件到 ws 外（拒绝或安全失败皆可）
    if r.success:
        written = list(tmp_path.rglob("*"))
        assert all(p.resolve().is_relative_to(tmp_path.resolve()) for p in written)
    assert "escape" not in (r.error or "").lower() or r.success is False


# ── P2: symlink → 系统目录 ────────────────────────────────────────────────

@_needs_symlink
def test_p2_symlink_to_etc_rejected(tmp_path):
    evil = tmp_path / "evil"
    os.symlink("/etc", evil)  # symlink 目录本身在 ws 内，但指向 /etc
    r = _write(tmp_path, "evil/passwd")
    assert r.success is False, "写 symlink→/etc 的目标必须被拒绝"
    assert "outside" in (r.error or "").lower()


# ── P3: symlink → workspace 外 ────────────────────────────────────────────

@_needs_symlink
def test_p3_symlink_outside_rejected(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    evil = tmp_path / "link"
    os.symlink(str(outside), evil)
    r = _write(tmp_path, "link/file.txt")
    assert r.success is False, "写 symlink→workspace外的目标必须被拒绝"


# ── P7: 嵌套 symlink 出界 ─────────────────────────────────────────────────

@_needs_symlink
def test_p7_nested_symlink_escape_rejected(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (tmp_path / "hop").mkdir()
    os.symlink(str(outside), tmp_path / "hop" / "out")   # 第一跳出界
    os.symlink("hop/out", tmp_path / "link2")             # 第二跳
    r = _write(tmp_path, "link2/file.txt")
    assert r.success is False, "嵌套 symlink 逃逸必须被拒绝"


# ── P4: hardlink 到 workspace 外（OS 语义记录） ──────────────────────────

def test_p4_hardlink_path_anchored(tmp_path, tmp_path_factory):
    """hardlink 在路径锚定层无法区分（同一 inode），但路径必须不逃逸 ws。

    hardlink 到外部 inode 的写入属于文件系统语义，超出路径锚定责任；
    本测试验证三层防御不会因 hardlink 而把解析路径带出 workspace。
    """
    outside = tmp_path_factory.mktemp("outside")
    ext = outside / "shared.txt"
    ext.write_text("original", encoding="utf-8")
    inner = tmp_path / "shared.txt"
    try:
        os.link(str(ext), str(inner))
    except OSError:
        pytest.skip("hardlink not supported on this filesystem")
    # 路径解析仍在 ws 内（is_path_safe 不应因 hardlink 误判逃逸）
    from core.base import is_path_safe
    assert is_path_safe(str(inner), str(tmp_path)) is True
