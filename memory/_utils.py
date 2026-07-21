"""memory/_utils.py — 记忆系统内部工具函数（从 store.py 提取）。"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path
from typing import Any

import yaml

from memory.models import Memory, MemoryMetadata, MemoryScope, MemoryStatus, MemoryType, parse_memory_type
from utils.frontmatter import parse_frontmatter as _parse_frontmatter

logger = logging.getLogger(__name__)

_MAX_INDEX_LINES = 200
_MAX_INDEX_BYTES = 25_600


def build_frontmatter(memory: Memory) -> str:
    """从 Memory 对象生成 YAML frontmatter 字符串。"""
    fm: dict[str, Any] = {
        "name": memory.name,
        "description": memory.description,
        "type": memory.metadata.type.value,
        "updated_at": memory.updated_at,
    }
    meta: dict[str, Any] = {}
    if memory.metadata.status is not MemoryStatus.ACTIVE:
        meta["status"] = memory.metadata.status.value
    if memory.metadata.access_count > 0:
        meta["access_count"] = memory.metadata.access_count
    if memory.metadata.validated_at:
        meta["validated_at"] = memory.metadata.validated_at
    if memory.metadata.scope is not MemoryScope.PROJECT:
        meta["scope"] = memory.metadata.scope.value
    if memory.metadata.confidence != 0.7:
        meta["confidence"] = memory.metadata.confidence
    if memory.metadata.ttl_seconds is not None:
        meta["ttl_seconds"] = memory.metadata.ttl_seconds
    if memory.metadata.expires_at:
        meta["expires_at"] = memory.metadata.expires_at
    if meta:
        fm["metadata"] = meta
    if memory.anchors:
        fm["anchors"] = [a.to_dict() for a in memory.anchors]
    return yaml.dump(fm, default_flow_style=False, allow_unicode=True).strip()


def build_memory_file(memory: Memory) -> str:
    """组装完整的记忆文件内容（frontmatter + body）。"""
    fm = build_frontmatter(memory)
    return f"---\n{fm}\n---\n\n{memory.content.strip()}\n"


def atomic_write_text(path: Path, content: str) -> None:
    """Write text via temp file + os.replace() to avoid torn reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(
        f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}"
    )  # P2-42: include thread ID to prevent intra-process collision
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def needs_type_migration(frontmatter: dict[str, Any]) -> bool:
    """Check if memory frontmatter needs type field migration."""
    top_level_type = frontmatter.get("type")
    metadata = frontmatter.get("metadata")
    metadata_has_type = isinstance(metadata, dict) and "type" in metadata
    if metadata_has_type:
        return True
    if top_level_type:
        try:
            MemoryType(top_level_type)
            return False
        except ValueError:
            from memory.models import _OLD_TYPE_MAP
            return top_level_type in _OLD_TYPE_MAP
    return not top_level_type


def truncate_index(
    content: str,
    max_lines: int = _MAX_INDEX_LINES,
    max_bytes: int = _MAX_INDEX_BYTES,
) -> str:
    """
    Truncate MEMORY.md content to 200-line / 25KB limits.

    CC-aligned: first truncate by lines, then by bytes, append WARNING.
    """
    original = content
    lines = content.splitlines()

    if len(lines) > max_lines:
        lines = lines[:max_lines]
        content = "\n".join(lines)

    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
        last_newline = truncated.rfind("\n")
        content = truncated[:last_newline] if last_newline > 0 else truncated

    if content != original:
        logger.warning(
            "MEMORY.md truncated: %d→%d lines, %d→%d bytes. "
            "Consider running consolidation to reduce index size.",
            len(original.splitlines()), len(content.splitlines()),
            len(original.encode("utf-8")), len(content.encode("utf-8")),
        )
        content += (
            "\n\n> WARNING: MEMORY.md is truncated. Only part of it was loaded."
            "\n> Run consolidation to reduce the index size."
        )

    return content


def project_hash(repo_path: str) -> str:
    """从项目路径生成短哈希，用于隔离不同项目的记忆目录。"""
    return hashlib.sha256(repo_path.encode("utf-8")).hexdigest()[:12]
