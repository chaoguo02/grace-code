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
from enum import Enum
from typing import Any


class MemoryType(str, Enum):
    """Typed memory categories. String-valued for JSON/YAML interop."""
    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"


class MemoryStatus(str, Enum):
    """Typed lifecycle status. Code is Truth — status drives injection."""
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class MemoryScope(str, Enum):
    """Typed visibility scope for memory routing."""
    SESSION = "session"
    PROJECT = "project"
    GLOBAL = "global"


# Old 3-type system → new 4-type system (for reading legacy files)
_OLD_TYPE_MAP: dict[str, str] = {
    "episodic": "user",
    "procedural": "feedback",
    "semantic": "project",
}

# Injection strategy constants (aligned with Claude Code)
ALWAYS_INJECT_TYPES: frozenset[MemoryType] = frozenset({MemoryType.USER, MemoryType.FEEDBACK})
ON_DEMAND_TYPES: frozenset[MemoryType] = frozenset({MemoryType.PROJECT, MemoryType.REFERENCE})
GLOBAL_TYPES: frozenset[MemoryType] = frozenset({MemoryType.USER, MemoryType.FEEDBACK})


@dataclass
class Anchor:
    """记忆锚点：将记忆关联到文件、符号或任务类型。

    content_hash: SHA256 of the anchored file content at write time.
    When the file changes, the hash mismatches → memory is physically
    discarded at injection time. Code is Truth.
    """
    kind: str
    path: str | None = None
    name: str | None = None
    value: str | None = None
    content_hash: str = ""  # SHA256 hex — empty = no hash binding

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key in ("kind", "path", "name", "value"):
            v = getattr(self, key)
            if v is not None:
                result[key] = v
        if self.content_hash:
            result["content_hash"] = self.content_hash
        return result


@dataclass
class MemoryMetadata:
    """记忆元数据。

    status lifecycle:
      ACTIVE     — memory is current, inject into context
      DEPRECATED — explicitly invalidated (superseded by code change, manual /deprecate command)
                   NOT injected into context. Code is Truth.

    scope: where the memory applies:
      SESSION  — only this session (cleared on exit)
      PROJECT  — this project (shared across sessions)
      GLOBAL   — all projects (user preferences, universal rules)

    confidence: 0.0–1.0, how confident we are in this memory.
      - 1.0: confirmed by user explicitly
      - 0.7–0.9: extracted by LLM with high confidence
      - 0.3–0.6: extracted by LLM with medium confidence, subject to verification
      - < 0.3: low confidence — not injected, pending validation

    ttl_seconds: time-to-live in seconds. None = permanent (default for user/feedback).
      Project/reference memories may have shorter TTLs.
    """
    type: MemoryType = MemoryType.PROJECT
    status: MemoryStatus = MemoryStatus.ACTIVE
    scope: MemoryScope = MemoryScope.PROJECT
    confidence: float = 0.7  # 0.0–1.0
    ttl_seconds: int | None = None  # None = permanent
    expires_at: str = ""  # computed ISO timestamp when TTL expires
    access_count: int = 0
    validated_at: str = ""

    def __post_init__(self) -> None:
        """Normalize string values to their enum equivalents for backward compat."""
        if isinstance(self.type, str) and not isinstance(self.type, MemoryType):
            object.__setattr__(self, "type", normalize_memory_type(self.type))
        if isinstance(self.status, str) and not isinstance(self.status, MemoryStatus):
            try:
                object.__setattr__(self, "status", MemoryStatus(self.status))
            except ValueError:
                object.__setattr__(self, "status", MemoryStatus.ACTIVE)
        if isinstance(self.scope, str) and not isinstance(self.scope, MemoryScope):
            try:
                object.__setattr__(self, "scope", MemoryScope(self.scope))
            except ValueError:
                object.__setattr__(self, "scope", MemoryScope.PROJECT)


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
            "status": self.metadata.status,
            "scope": self.metadata.scope,
            "confidence": self.metadata.confidence,
            "ttl_seconds": self.metadata.ttl_seconds,
            "expires_at": self.metadata.expires_at,
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


def normalize_memory_type(raw_type: str | None) -> MemoryType:
    """
    Normalize memory type to the 4-type enum.

    Handles:
    - None/empty → MemoryType.PROJECT (default)
    - Old 3-type names (episodic/semantic/procedural) → mapped to new equivalents
    - Valid new type names → pass through
    - Unknown → MemoryType.PROJECT (default)
    """
    if not raw_type:
        return MemoryType.PROJECT
    try:
        return MemoryType(raw_type)
    except ValueError:
        pass
    mapped = _OLD_TYPE_MAP.get(raw_type)
    if mapped:
        return MemoryType(mapped)
    return MemoryType.PROJECT


def parse_memory_type(frontmatter: dict[str, Any]) -> MemoryType:
    """
    Parse memory type from frontmatter, preferring Claude Code's top-level type.

    Compatibility order:
    1. top-level `type`
    2. legacy `metadata.type`
    3. default MemoryType.PROJECT
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

    return MemoryType.PROJECT


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
