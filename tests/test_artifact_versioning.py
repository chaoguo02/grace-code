"""Phase 3B: ArtifactStore 版本化 + 不可变语义 — Target 测试。

对齐 Claude Code "对话历史不可变"：同一工具的新 artifact 获得递增版本
(v1, v2, ...)，旧版本保留并标记 superseded，引用始终指向最新版本。
"""

from __future__ import annotations

import pytest

from context.artifacts import Artifact, ArtifactStore


def test_overwrite_creates_new_version_preserving_old(tmp_path):
    """同 tool 连续产出 → 版本递增，旧版本保留且 superseded。"""
    store = ArtifactStore(threshold_tokens=1, storage_dir=str(tmp_path))

    a1 = store.store("Bash", "output one " + "x" * 100)
    a2 = store.store("Bash", "output two " + "y" * 100)

    assert a1 is not None and a2 is not None
    # 版本递增
    assert a1.version == 1
    assert a2.version == 2
    # 旧版本保留且被标记 superseded
    assert a1.superseded is True
    assert a2.superseded is False
    # 两个 artifact 都在 store 中（不可变，未被覆盖）
    assert store.get(a1.artifact_id) is not None
    assert store.get(a2.artifact_id) is not None


def test_different_tools_versions_independent():
    """不同工具的版本计数互不影响。"""
    store = ArtifactStore(threshold_tokens=1)
    store.store("Bash", "b1 " + "x" * 100)
    store.store("Bash", "b2 " + "y" * 100)
    store.store("Read", "r1 " + "z" * 100)

    bash_arts = [a for a in store._store.values() if a.tool_name == "Bash"]
    read_arts = [a for a in store._store.values() if a.tool_name == "Read"]
    assert sorted(a.version for a in bash_arts) == [1, 2]
    assert [a.version for a in read_arts] == [1]


def test_reference_text_shows_version():
    """reference_text 对 v2+ 显示版本标记。"""
    store = ArtifactStore(threshold_tokens=1)
    a1 = store.store("Bash", "one " + "x" * 100)
    a2 = store.store("Bash", "two " + "y" * 100)
    assert " v1" not in a1.reference_text()  # v1 不显示（向后兼容）
    assert " v2" in a2.reference_text()
    assert "[superseded]" in a1.reference_text()


def test_from_dict_backward_compatible_without_version():
    """旧数据（无 version 字段）→ 默认 version=1, superseded=False。"""
    art = Artifact.from_dict({
        "artifact_id": "art_abc",
        "tool_name": "Bash",
        "full_content": "c",
        "summary": "s",
        "token_count": 1,
        "char_count": 1,
        "original_length": 1,
        "line_count": 1,
    })
    assert art.version == 1
    assert art.superseded is False


def test_persist_load_preserves_versions(tmp_path):
    """磁盘持久化往返保留版本号和 superseded 标记。"""
    store = ArtifactStore(threshold_tokens=1, storage_dir=str(tmp_path))
    a1 = store.store("Bash", "one " + "x" * 100)
    store.store("Bash", "two " + "y" * 100)

    # 重新加载
    store2 = ArtifactStore(threshold_tokens=1, storage_dir=str(tmp_path))
    loaded = store2.get(a1.artifact_id)
    assert loaded is not None
    assert loaded.version == 1
    assert loaded.superseded is True
