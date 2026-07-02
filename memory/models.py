"""
memory/models.py

记忆数据模型。

记忆是带 YAML frontmatter 的 Markdown 文件，格式参照 Claude Code 的 auto memory：
- 每个文件一条记忆
- MEMORY.md 作为索引，启动时注入上下文
- 主题文件按需读取
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

MemoryType = Literal["user", "feedback", "project", "reference"]

# Old 3-type system → new 4-type system (for reading legacy files)
_OLD_TYPE_MAP: dict[str, str] = {
    "episodic": "user",
    "procedural": "feedback",
    "semantic": "project",
}
_VALID_MEMORY_TYPES = frozenset({"user", "feedback", "project", "reference"})

# Injection strategy constants (aligned with Claude Code)
ALWAYS_INJECT_TYPES = frozenset({"user", "feedback"})
ON_DEMAND_TYPES = frozenset({"project", "reference"})
GLOBAL_TYPES = frozenset({"user", "feedback"})


@dataclass
class Anchor:
    """记忆锚点：将记忆关联到文件、符号或任务类型。"""
    kind: str
    path: str | None = None
    name: str | None = None
    value: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key in ("kind", "path", "name", "value")
            if (value := getattr(self, key)) is not None
        }


@dataclass
class MemoryMetadata:
    """记忆元数据。"""
    type: str = "project"  # "user" | "feedback" | "project" | "reference"
    stale: bool = False
    access_count: int = 0
    validated_at: str = ""


@dataclass
class Memory:
    """
    单条记忆。

    name 是 slug（短横线命名），同时也是文件名（{name}.md）。
    description 是一行摘要，LLM 用它判断是否相关。
    content 是 markdown 正文。
    anchors 将记忆绑定到文件、符号或任务类型，用于精确检索。
    """
    name: str
    description: str
    content: str
    metadata: MemoryMetadata = field(default_factory=MemoryMetadata)
    updated_at: str = field(default_factory=lambda: _now())
    anchors: list[Anchor] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "type": self.metadata.type,
            "updated_at": self.updated_at,
            "content": self.content,
            "anchors": [anchor.to_dict() for anchor in self.anchors],
        }


@dataclass
class MemorySummary:
    """
    记忆摘要（不含正文），用于列表和索引。
    MEMORY.md 中的每一行对应一个 MemorySummary。
    """
    name: str
    description: str
    type: str
    updated_at: str = ""


def normalize_memory_type(raw_type: str | None) -> str:
    """
    Normalize memory type to the 4-type system (user/feedback/project/reference).

    Handles:
    - None/empty → "project" (default)
    - Old 3-type names (episodic/semantic/procedural) → mapped to new equivalents
    - Valid new type names → pass through
    - Unknown → "project" (default)
    """
    if not raw_type:
        return "project"
    if raw_type in _VALID_MEMORY_TYPES:
        return raw_type
    mapped = _OLD_TYPE_MAP.get(raw_type)
    if mapped:
        return mapped
    return "project"


def parse_memory_type(frontmatter: dict[str, Any]) -> str:
    """
    Parse memory type from frontmatter, preferring Claude Code's top-level type.

    Compatibility order:
    1. top-level `type`
    2. legacy `metadata.type`
    3. default `project`
    """
    top_level_type = frontmatter.get("type")
    if top_level_type:
        return normalize_memory_type(str(top_level_type))

    metadata = frontmatter.get("metadata")
    if isinstance(metadata, dict):
        metadata_type = metadata.get("type")
        if metadata_type:
            return normalize_memory_type(str(metadata_type))
    elif isinstance(metadata, str):
        return normalize_memory_type(metadata)

    return "project"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
