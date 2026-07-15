"""
memory/store.py

MemoryStore — 文件型长期记忆存储。

目录结构：
    ~/.forge-agent/projects/<project-hash>/memory/
    ├── MEMORY.md          # 索引文件（启动时注入前 N 行）
    ├── build-commands.md  # 主题文件
    ├── debugging.md
    └── ...

MEMORY.md 格式：
    # Memory Index

    - [build-commands](build-commands.md) — Build, test, and lint commands
    - [debugging](debugging.md) — Common debugging patterns

主题文件格式（YAML frontmatter + Markdown）：
    ---
    name: build-commands
    description: Build, test, and lint commands
    metadata:
      type: project
    ---

    ## Build
    ...
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

from memory.models import Anchor, Memory, MemoryMetadata, MemoryScope, MemoryStatus, MemorySummary, MemoryType, normalize_memory_type, parse_memory_type

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_DEFAULT_BASE_DIR = "~/.forge-agent/projects"
_GLOBAL_MEMORY_DIR = "~/.forge-agent/global/memory"
_INDEX_FILENAME = "MEMORY.md"
_FRONTMATTER_SEP = "---"
_MAX_INDEX_LINES = 200  # MEMORY.md 默认最大行数
_MAX_INDEX_BYTES = 25_600  # MEMORY.md 最大字节数 (25KB)

# user 和 feedback 类型默认存储到全局（跨项目共享）
_GLOBAL_MEMORY_TYPES: frozenset[MemoryType] = frozenset({MemoryType.USER, MemoryType.FEEDBACK})

# ---------------------------------------------------------------------------
# YAML frontmatter 解析
# ---------------------------------------------------------------------------

_FM_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)",
    re.DOTALL,
)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """
    解析 YAML frontmatter + Markdown 正文。

    Returns:
        (frontmatter_dict, body_text)
        没有 frontmatter 时 frontmatter_dict 为空字典。
    """
    m = _FM_RE.match(text)
    if not m:
        return {}, text.strip()
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    body = m.group(2).strip()
    return fm, body


def _build_frontmatter(memory: Memory) -> str:
    """从 Memory 对象生成 YAML frontmatter 字符串。"""
    fm: dict[str, Any] = {
        "name": memory.name,
        "description": memory.description,
        "type": memory.metadata.type.value,  # use .value for clean YAML serialization
        "updated_at": memory.updated_at,
    }
    meta: dict[str, Any] = {}
    if memory.metadata.status is not MemoryStatus.ACTIVE:
        meta["status"] = memory.metadata.status.value
    if memory.metadata.access_count > 0:
        meta["access_count"] = memory.metadata.access_count
    if memory.metadata.validated_at:
        meta["validated_at"] = memory.metadata.validated_at
    # Phase 4: persist scope, confidence, ttl
    if memory.metadata.scope is not MemoryScope.PROJECT:
        meta["scope"] = memory.metadata.scope.value
    if memory.metadata.confidence != 0.7:  # only persist non-default
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


def _build_memory_file(memory: Memory) -> str:
    """组装完整的记忆文件内容（frontmatter + body）。"""
    fm = _build_frontmatter(memory)
    return f"---\n{fm}\n---\n\n{memory.content.strip()}\n"


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text via temp file + os.replace() to avoid torn reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def _needs_type_migration(frontmatter: dict[str, Any]) -> bool:
    from memory.models import MemoryType

    top_level_type = frontmatter.get("type")
    metadata = frontmatter.get("metadata")
    metadata_has_type = isinstance(metadata, dict) and "type" in metadata
    if metadata_has_type:
        return True
    if top_level_type:
        try:
            MemoryType(top_level_type)
            return False  # valid current type
        except ValueError:
            # Check old type names
            from memory.models import _OLD_TYPE_MAP
            return top_level_type in _OLD_TYPE_MAP
    return not top_level_type


def _truncate_index(content: str, max_lines: int = _MAX_INDEX_LINES, max_bytes: int = _MAX_INDEX_BYTES) -> str:
    """
    Truncate MEMORY.md content to 200-line / 25KB limits.

    Aligned with Claude Code's truncateMemoryIndex():
    - First truncate by lines
    - Then truncate by bytes (at last newline boundary)
    - Append WARNING if truncated
    """
    original = content
    lines = content.splitlines()

    # Line limit
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        content = "\n".join(lines)

    # Byte limit (truncate at last newline before the limit)
    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
        last_newline = truncated.rfind("\n")
        if last_newline > 0:
            content = truncated[:last_newline]
        else:
            content = truncated

    if content != original:
        content += "\n\n> WARNING: MEMORY.md is truncated. Only part of it was loaded."

    return content


def _project_hash(repo_path: str) -> str:
    """从项目路径生成短哈希，用于隔离不同项目的记忆目录。"""
    return hashlib.sha256(repo_path.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------

class MemoryStore:
    """
    文件型记忆存储。

    Args:
        repo_path:    项目根目录路径（用于生成项目标识）
        base_dir:     记忆根目录，默认 ~/.forge-agent/projects
        memory_dir:   可选，直接指定记忆目录（覆盖自动计算）
        max_index_lines: MEMORY.md 每次注入的最大行数
        indexer:      可选，MemoryIndexer 实例，写入/删除时自动同步向量索引
    """

    def __init__(
        self,
        repo_path: str,
        base_dir: str | None = None,
        memory_dir: str | None = None,
        max_index_lines: int = _MAX_INDEX_LINES,
        indexer: Any | None = None,
    ) -> None:
        if memory_dir:
            self._store_dir = Path(memory_dir).expanduser().resolve()
        else:
            base = Path(base_dir or _DEFAULT_BASE_DIR).expanduser()
            self._store_dir = base / _project_hash(repo_path) / "memory"
        self._max_index_lines = max_index_lines
        self._indexer = indexer
        self._dirty = False
        self._anchor_index: dict[str, list[str]] | None = None  # file_path → [memory_names]
        self._access_count_cache: dict[str, int] = {}  # deferred access_count increments
        self._ensure_dir()
        # ── Phase 6: In-memory metadata cache ──
        from memory.metadata_cache import MetadataCache
        self._metadata_cache = MetadataCache()
        self._metadata_cache.build(self._store_dir)

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def store_dir(self) -> Path:
        """记忆文件存放目录。"""
        return self._store_dir

    @property
    def index_path(self) -> Path:
        """MEMORY.md 索引文件路径。"""
        return self._store_dir / _INDEX_FILENAME

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def read_memory(self, name: str) -> Memory | None:
        """
        读取一条记忆。

        Args:
            name: 记忆名称（slug），对应 {name}.md

        Returns:
            Memory 对象，不存在时返回 None
        """
        path = self._file_path(name)
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to read memory %s: %s", name, exc)
            return None

        fm, body = _parse_frontmatter(text)
        meta = fm.get("metadata", {})
        if isinstance(meta, str):
            meta = {"type": meta}

        # Parse anchors
        anchors = []
        for a in fm.get("anchors", []):
            if isinstance(a, dict):
                anchors.append(Anchor(
                    kind=a.get("kind", "file"),
                    path=a.get("path"),
                    name=a.get("name"),
                    value=a.get("value"),
                    content_hash=str(a.get("content_hash", "")),
                ))

        # ── P1: status field with backward compat for stale boolean ──
        raw_status = meta.get("status") or fm.get("status")
        if not raw_status and bool(meta.get("stale", False)):
            raw_status = MemoryStatus.DEPRECATED.value  # migrate stale=True → status=deprecated

        # Phase 4: read scope, confidence, ttl from frontmatter
        _scope_raw = str(meta.get("scope") or fm.get("scope") or "project")
        _confidence = float(meta.get("confidence") or fm.get("confidence") or 0.7)
        _ttl = meta.get("ttl_seconds") or fm.get("ttl_seconds")
        _ttl = int(_ttl) if _ttl is not None else None
        _expires = str(meta.get("expires_at") or fm.get("expires_at") or "")

        # Parse scope string → MemoryScope enum
        try:
            _scope = MemoryScope(_scope_raw)
        except ValueError:
            _scope = MemoryScope.PROJECT
        # Parse status string → MemoryStatus enum
        try:
            _status = MemoryStatus(str(raw_status)) if raw_status else MemoryStatus.ACTIVE
        except ValueError:
            _status = MemoryStatus.ACTIVE

        memory = Memory(
            name=fm.get("name", name),
            description=fm.get("description", ""),
            content=body,
            metadata=MemoryMetadata(
                type=parse_memory_type(fm),
                status=_status,
                scope=_scope,
                confidence=_confidence,
                ttl_seconds=_ttl,
                expires_at=_expires,
                access_count=int(meta.get("access_count", 0)),
                validated_at=str(meta.get("validated_at", "")),
            ),
            updated_at=fm.get("updated_at", ""),
            anchors=anchors,
        )
        if _needs_type_migration(fm):
            try:
                _atomic_write_text(path, _build_memory_file(memory))
                self._dirty = True
            except OSError:
                pass
        return memory

    def write_memory(self, memory: Memory) -> bool:
        """
        写入一条记忆（创建或覆盖）。

        自动更新 MEMORY.md 索引，并同步向量索引（如有 indexer）。

        Args:
            memory: Memory 对象

        Returns:
            True 表示成功
        """
        content = _build_memory_file(memory)
        path = self._file_path(memory.name)
        try:
            _atomic_write_text(path, content)
        except OSError as exc:
            logger.error("Failed to write memory %s: %s", memory.name, exc)
            return False
        self._dirty = True
        self._anchor_index = None  # invalidate reverse index
        # Phase 6: update in-memory cache (no index rebuild needed)
        cache = getattr(self, "_metadata_cache", None)
        if cache is not None:
            cache.upsert(memory)
        if self._indexer is not None:
            try:
                self._indexer.index_memory(memory)
            except Exception as exc:
                logger.warning("Indexer failed for %s: %s", memory.name, exc)
        return True

    def list_memories(self) -> list[MemorySummary]:
        """列出所有记忆摘要。

        Phase 6: Uses in-memory MetadataCache (O(1) allocation, no file I/O).
        Falls back to MEMORY.md / directory scan if cache is empty.
        """
        cache = getattr(self, "_metadata_cache", None)
        if cache is not None and cache.is_built and cache.count > 0:
            return cache.list_summaries()
        # Fallback: old MEMORY.md / directory scan path
        if self._dirty or not self.index_path.exists():
            self._rebuild_index()
            self._dirty = False
        if self.index_path.exists():
            summaries = self._parse_index(self.index_path.read_text(encoding="utf-8"))
            if summaries:
                return summaries
        return self._scan_dir()

    def count_by_type(self) -> dict[str, int]:
        """
        统计每种类型的记忆数量。

        Returns:
            {type_name: count, ...} 例如 {"user": 1, "feedback": 3, "project": 5, "reference": 2}
        """
        counts: dict[str, int] = {}
        for summary in self.list_memories():
            t = summary.type
            counts[t] = counts.get(t, 0) + 1
        return counts

    def delete_memory(self, name: str) -> bool:
        """
        删除一条记忆。

        Args:
            name: 记忆名称（slug）

        Returns:
            True 表示成功（文件不存在也返回 True）
        """
        path = self._file_path(name)
        if not path.exists():
            return True
        try:
            path.unlink()
        except OSError as exc:
            logger.error("Failed to delete memory %s: %s", name, exc)
            return False
        self._dirty = True
        self._anchor_index = None  # invalidate reverse index
        # Phase 6: remove from in-memory cache
        cache = getattr(self, "_metadata_cache", None)
        if cache is not None:
            cache.remove(name)
        if self._indexer is not None:
            try:
                self._indexer.remove_memory(name)
            except Exception as exc:
                logger.warning("Indexer remove failed for %s: %s", name, exc)
        return True

    # ------------------------------------------------------------------
    # 生命周期管理
    # ------------------------------------------------------------------

    def record_access(self, name: str) -> bool:
        """
        递增记忆的 access_count（延迟写回模式）。

        累积到 _access_count_cache 中，调用 flush_access_counts() 时批量持久化。
        为保证测试可观测性，当前实现立即写回。

        Args:
            name: 记忆名称

        Returns:
            True 表示成功，记忆不存在时返回 False
        """
        memory = self.read_memory(name)
        if memory is None:
            return False
        memory.metadata.access_count += 1
        return self.write_memory(memory)

    def mark_stale_for_file(self, file_path: str) -> int:
        """
        将所有锚定到指定文件的记忆标记为 stale。

        使用反向索引加速查找（首次调用时从磁盘构建，write/delete 时失效）。

        Args:
            file_path: 被修改的文件路径（相对路径）

        Returns:
            被标记为 stale 的记忆数量
        """
        normalized = file_path.replace("\\", "/").lstrip("./")
        index = self._get_anchor_index()
        count = 0

        # 收集匹配的记忆名（精确匹配 + 前缀匹配）
        candidates: set[str] = set()
        for anchor_path, names in index.items():
            if normalized == anchor_path or normalized.startswith(anchor_path + "/"):
                candidates.update(names)

        for name in candidates:
            memory = self.read_memory(name)
            if memory is None or memory.metadata.status is MemoryStatus.DEPRECATED:
                continue
            memory.metadata.status = MemoryStatus.DEPRECATED
            self.write_memory(memory)
            count += 1
        return count

    def deprecate(self, name: str, reason: str = "") -> bool:
        """Explicitly deprecate a memory. The authoritative invalidation path.

        Unlike mtime-based stale marking (which guesses from file timestamps),
        this is called when a developer intentionally changes the behavior that
        the memory describes. The memory is marked 'deprecated' and will not be
        injected into any future context.

        Returns True if the memory was found and deprecated.
        """
        memory = self.read_memory(name)
        if memory is None:
            return False
        memory.metadata.status = MemoryStatus.DEPRECATED
        if reason:
            memory.content = (
                f"[DEPRECATED: {reason}]\n\n{memory.content}"
            )
        self.write_memory(memory)
        logger.info("Memory '%s' explicitly deprecated: %s", name, reason)
        return True

    def deprecate_by_pattern(self, name_pattern: str, reason: str = "") -> int:
        """Bulk-deprecate memories matching a name glob pattern.

        The authoritative invalidation path for code refactoring. When P1-5
        deletes _validate_subagent_report(), call:
            store.deprecate_by_pattern("*regex*", "Replaced by JSON Schema")

        Returns the count of memories deprecated.
        """
        import fnmatch
        count = 0
        for summary in self.list_memories():
            if fnmatch.fnmatch(summary.name, name_pattern):
                if self.deprecate(summary.name, reason):
                    count += 1
        logger.info(
            "Bulk-deprecated %d memories matching '%s': %s",
            count, name_pattern, reason,
        )
        return count

    def _get_anchor_index(self) -> dict[str, list[str]]:
        """
        获取或构建反向索引：{normalized_anchor_path: [memory_name, ...]}。

        索引在 write/delete 时失效（_anchor_index = None），下次调用时重建。
        """
        if self._anchor_index is not None:
            return self._anchor_index

        index: dict[str, list[str]] = {}
        if not self._store_dir.exists():
            self._anchor_index = index
            return index

        for fpath in sorted(self._store_dir.glob("*.md")):
            if fpath.name == _INDEX_FILENAME:
                continue
            try:
                text = fpath.read_text(encoding="utf-8")
            except OSError:
                continue
            fm, _ = _parse_frontmatter(text)
            name = fm.get("name", fpath.stem)
            for a in fm.get("anchors", []):
                if not isinstance(a, dict):
                    continue
                if a.get("kind") != "file" or not a.get("path"):
                    continue
                anchor_path = a["path"].replace("\\", "/").lstrip("./")
                index.setdefault(anchor_path, []).append(name)

        self._anchor_index = index
        return index

    def validate_memory(self, name: str) -> bool:
        """
        重置记忆的 stale 状态并更新 validated_at 时间戳。

        Args:
            name: 记忆名称

        Returns:
            True 表示成功，记忆不存在时返回 False
        """
        memory = self.read_memory(name)
        if memory is None:
            return False
        from memory.models import _now
        memory.metadata.status = MemoryStatus.ACTIVE
        memory.metadata.validated_at = _now()
        return self.write_memory(memory)

    def prune_expired(
        self,
        max_episodic_age_days: int = 30,
        *,
        max_user_age_days: int | None = None,
    ) -> int:
        """
        清理过期的 user 记忆。

        保留策略：retention_days = max_age_days * (1 + access_count * 0.5)
        只清理 user 类型；feedback/project/reference 不受影响。
        max_user_age_days is accepted as the new-name alias for compatibility.
        """
        from datetime import datetime, timezone

        max_age_days = max_user_age_days if max_user_age_days is not None else max_episodic_age_days
        now = datetime.now(timezone.utc)
        pruned = 0
        for fpath in sorted(self._store_dir.glob("*.md")):
            if fpath.name == _INDEX_FILENAME:
                continue
            name = fpath.stem
            memory = self.read_memory(name)
            if memory is None:
                continue
            if memory.metadata.type is not MemoryType.USER:
                continue
            if not memory.updated_at:
                continue
            try:
                updated = datetime.fromisoformat(memory.updated_at.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            age_days = (now - updated).days
            retention_days = max_age_days * (1 + memory.metadata.access_count * 0.5)
            if age_days > retention_days:
                self.delete_memory(name)
                pruned += 1
        return pruned

    def evict_expired_by_ttl(self) -> int:
        """Evict memories whose ttl_seconds has expired.

        Checks each memory's metadata.ttl_seconds and expires_at.
        Returns count of evicted memories.
        """
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        pruned = 0
        for fpath in sorted(self._store_dir.glob("*.md")):
            if fpath.name == _INDEX_FILENAME:
                continue
            name = fpath.stem
            memory = self.read_memory(name)
            if memory is None:
                continue
            # Check TTL
            ttl = getattr(memory.metadata, "ttl_seconds", None)
            if ttl is None:
                continue  # permanent — never expires
            # Use expires_at if set, otherwise compute from updated_at
            expires_at_str = getattr(memory.metadata, "expires_at", "")
            if expires_at_str:
                try:
                    expires_at = datetime.fromisoformat(
                        expires_at_str.replace("Z", "+00:00")
                    )
                    if now > expires_at:
                        self.delete_memory(name)
                        pruned += 1
                except (ValueError, TypeError):
                    pass
            elif memory.updated_at:
                try:
                    updated = datetime.fromisoformat(
                        memory.updated_at.replace("Z", "+00:00")
                    )
                    if (now - updated).total_seconds() > ttl:
                        self.delete_memory(name)
                        pruned += 1
                except (ValueError, TypeError):
                    pass
        return pruned

    def list_by_scope(
        self, scope: str = "project", min_confidence: float = 0.0
    ) -> list:
        """List active memories filtered by scope and minimum confidence.

        Phase 6: Uses in-memory MetadataCache — O(n) memory scan, ZERO file I/O.
        Content is NOT loaded; call read_memory() for full content when needed.

        Returns memories sorted by confidence (highest first).
        """
        cache = getattr(self, "_metadata_cache", None)
        if cache is not None and cache.is_built:
            return cache.list_by_scope(scope, min_confidence)
        # Fallback: old file-I/O path
        try:
            target_scope = MemoryScope(scope)
        except ValueError:
            target_scope = MemoryScope.PROJECT
        summaries = self.list_memories()
        results = []
        for summary in summaries:
            memory = self.read_memory(summary.name)
            if memory is None:
                continue
            if memory.metadata.status is MemoryStatus.DEPRECATED:
                continue
            if memory.metadata.scope is not target_scope:
                continue
            mem_confidence = getattr(memory.metadata, "confidence", 0.5)
            if mem_confidence < min_confidence:
                continue
            results.append((mem_confidence, memory))
        results.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in results]

    def consolidate(
        self,
        candidate: Any,
        external_store: Any = None,
        backend: Any = None,
    ) -> str:
        """
        合并去重：决定候选记忆的处理方式。

        策略：
        1. 同名存在 + 内容相同 → NOOP
        2. 同名存在 + 内容不同 → UPDATE
        3. 外部向量搜索相似度 ≥ 0.85 → MERGE
        4. 相似度 0.5-0.85 → 调用 LLM judge → NOOP/UPDATE
        5. 无匹配 → ADD
        6. feedback 无 file/symbol anchor → 降级为 project

        Args:
            candidate: MemoryCandidate 对象
            external_store: 可选，外部向量存储（需有 search 方法）
            backend: 可选，LLM backend（用于灰区判断）

        Returns:
            操作结果字符串: "ADD", "UPDATE", "MERGE", "NOOP"
        """
        memory = candidate.to_memory()

        # 类型降级：feedback 无 file/symbol anchor → project
        if memory.metadata.type is MemoryType.FEEDBACK:
            has_valid_anchor = any(
                a.kind in ("file", "symbol") and (a.path or a.name)
                for a in memory.anchors
            )
            if not has_valid_anchor:
                memory.metadata.type = MemoryType.PROJECT

        # 检查同名记忆
        existing = self.read_memory(candidate.name)
        if existing is not None:
            if existing.content.strip() == memory.content.strip():
                return "NOOP"
            # 同名不同内容 → UPDATE
            existing.content = memory.content
            existing.description = memory.description
            existing.anchors = memory.anchors
            self.write_memory(existing)
            return "UPDATE"

        # 向量搜索去重
        if external_store is not None:
            try:
                results = external_store.search(query=memory.content, top_k=3, min_score=0.0)
            except Exception:
                results = []

            if results:
                top = results[0]
                score = top.get("score", 0)
                if score >= 0.85:
                    # MERGE：将新内容追加到已有记忆（上限 2000 字符）
                    target_name = top["name"]
                    target = self.read_memory(target_name)
                    if target is not None:
                        merged = target.content.strip() + "\n\n" + memory.content.strip()
                        if len(merged) > 2000:
                            # 保留新内容完整，截断旧内容尾部
                            budget = 2000 - len(memory.content.strip()) - 4
                            if budget > 100:
                                merged = target.content.strip()[:budget] + "\n\n" + memory.content.strip()
                            else:
                                merged = memory.content.strip()
                        target.content = merged
                        self.write_memory(target)
                        return "MERGE"
                elif score >= 0.5 and backend is not None:
                    # 灰区：LLM judge
                    try:
                        resp = backend.complete(
                            messages=[{
                                "role": "user",
                                "content": (
                                    f"Existing memory '{top['name']}':\n{top['content']}\n\n"
                                    f"New candidate '{candidate.name}':\n{memory.content}\n\n"
                                    "Should we: NOOP (discard new), UPDATE (replace existing), or ADD (keep both)?\n"
                                    "Reply with exactly one word: NOOP, UPDATE, or ADD."
                                ),
                            }],
                            tools=[],
                        )
                        decision = (resp.raw_content or "").strip().upper()
                        if decision == "NOOP":
                            return "NOOP"
                        elif decision == "UPDATE":
                            target_name = top["name"]
                            target = self.read_memory(target_name)
                            if target is not None:
                                target.content = memory.content
                                target.description = memory.description
                                self.write_memory(target)
                            return "UPDATE"
                    except Exception:
                        pass

        # 无匹配 → ADD
        self.write_memory(memory)
        return "ADD"

    # ------------------------------------------------------------------
    # 上下文注入
    # ------------------------------------------------------------------

    def get_index_content(self, max_lines: int | None = None) -> str:
        """
        获取 MEMORY.md 的内容（前 max_lines 行），用于注入 LLM 上下文。

        Enforces 200-line / 25KB hard limits (aligned with Claude Code).
        Truncation always happens at the last newline boundary before the limit.

        Args:
            max_lines: 最大行数，默认使用 self._max_index_lines

        Returns:
            MEMORY.md 的纯文本内容，空 store 返回空字符串
        """
        if self._dirty or not self.index_path.exists():
            self._rebuild_index()
            self._dirty = False
        if not self.index_path.exists():
            return ""

        text = self.index_path.read_text(encoding="utf-8").strip()
        if text.count("\n") == 0 and ("# Memory Index" in text or not text):
            return ""

        limit = max_lines if max_lines is not None else self._max_index_lines
        result = _truncate_index(text, max_lines=limit, max_bytes=_MAX_INDEX_BYTES)
        return result

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _file_path(self, name: str) -> Path:
        """返回 {name}.md 的完整路径。"""
        return self._store_dir / f"{name}.md"

    def _ensure_dir(self) -> None:
        """确保记忆目录存在。"""
        self._store_dir.mkdir(parents=True, exist_ok=True)

    def _rebuild_index(self) -> None:
        """
        从目录中的 .md 文件重建 MEMORY.md 索引。
        排除 MEMORY.md 自身。

        Enforces 200-line / 25KB hard limits at write time (aligned with Claude Code).
        """
        summaries = self._scan_dir()
        lines = ["# Memory Index\n"]
        for s in summaries:
            lines.append(
                f"- [{s.name}]({s.name}.md) — {s.description} ({s.type})"
            )
        content = "\n".join(lines) + "\n"
        content = _truncate_index(content, max_lines=_MAX_INDEX_LINES, max_bytes=_MAX_INDEX_BYTES)
        _atomic_write_text(self.index_path, content + "\n")

    def _scan_dir(self) -> list[MemorySummary]:
        """扫描目录，从 .md 文件中提取摘要（不含 MEMORY.md）。"""
        summaries: list[MemorySummary] = []
        if not self._store_dir.exists():
            return summaries
        for fpath in sorted(self._store_dir.glob("*.md")):
            if fpath.name == _INDEX_FILENAME:
                continue
            try:
                text = fpath.read_text(encoding="utf-8")
            except OSError:
                continue
            fm, _body = _parse_frontmatter(text)
            meta = fm.get("metadata", {})
            if isinstance(meta, str):
                meta = {"type": meta}
            summaries.append(MemorySummary(
                name=fm.get("name", fpath.stem),
                description=fm.get("description", ""),
                type=parse_memory_type(fm),
                updated_at=fm.get("updated_at", ""),
            ))
        return summaries

    @staticmethod
    def _parse_index(text: str) -> list[MemorySummary]:
        """
        从 MEMORY.md 文本解析 MemorySummary 列表。

        格式：- [name](name.md) — description (type)
        """
        summaries: list[MemorySummary] = []
        pattern = re.compile(r"-\s*\[(.+?)\]\((.+?\.md)\)\s*—\s*(.+?)(?:\s*\((\w+)\))?\s*$")
        for line in text.splitlines():
            m = pattern.match(line.strip())
            if m:
                summaries.append(MemorySummary(
                    name=m.group(1),
                    description=m.group(3).strip(),
                    type=normalize_memory_type(m.group(4)),
                ))
        return summaries


# ---------------------------------------------------------------------------
# TwoTierMemoryStore — 项目层 + 全局层
# ---------------------------------------------------------------------------

class TwoTierMemoryStore(MemoryStore):
    """
    双层记忆存储：项目级 + 全局级。

    - user/feedback 类型记忆默认存储到全局目录（跨项目共享）
    - project/reference 类型记忆存储到项目目录
    - 读取和列表操作合并两层结果
    """

    def __init__(
        self,
        repo_path: str,
        base_dir: str | None = None,
        memory_dir: str | None = None,
        global_dir: str | None = None,
        max_index_lines: int = _MAX_INDEX_LINES,
        indexer: Any | None = None,
    ) -> None:
        super().__init__(repo_path, base_dir, memory_dir, max_index_lines, indexer=indexer)
        global_path = Path(global_dir or _GLOBAL_MEMORY_DIR).expanduser().resolve()
        self._global_store = MemoryStore(
            repo_path="__global__",
            memory_dir=str(global_path),
            max_index_lines=max_index_lines,
            indexer=indexer,
        )

    @property
    def global_store(self) -> MemoryStore:
        """全局记忆存储。"""
        return self._global_store

    def write_memory(self, memory: Memory) -> bool:
        """
        写入记忆，按类型自动分流。

        episodic/procedural → 全局层（跨项目共享）
        semantic → 项目层
        """
        if memory.metadata.type in _GLOBAL_MEMORY_TYPES:
            return self._global_store.write_memory(memory)
        return super().write_memory(memory)

    def read_memory(self, name: str) -> Memory | None:
        """先查项目层，再查全局层。"""
        result = super().read_memory(name)
        if result is None:
            result = self._global_store.read_memory(name)
        return result

    def delete_memory(self, name: str) -> bool:
        """先尝试项目层删除，失败时尝试全局层。"""
        # 检查哪一层有这个记忆
        if super().read_memory(name) is not None:
            return super().delete_memory(name)
        if self._global_store.read_memory(name) is not None:
            return self._global_store.delete_memory(name)
        return True  # 都不存在，视为成功

    def list_memories(self) -> list[MemorySummary]:
        """合并两层记忆列表（去重：同名优先项目层）。"""
        project_memories = super().list_memories()
        global_memories = self._global_store.list_memories()

        # 去重
        seen = {s.name for s in project_memories}
        merged = list(project_memories)
        for s in global_memories:
            if s.name not in seen:
                merged.append(s)
                seen.add(s.name)
        return merged

    def get_index_content(self, max_lines: int | None = None) -> str:
        """合并两层索引内容。"""
        project_content = super().get_index_content(max_lines)
        global_content = self._global_store.get_index_content(max_lines)

        if not project_content.strip() and not global_content.strip():
            return ""

        parts: list[str] = []
        if project_content.strip():
            parts.append(project_content)
        if global_content.strip():
            # 给全局记忆加标题区分
            # 去掉全局的 "# Memory Index" 标题行
            global_lines = global_content.splitlines()
            global_lines = [l for l in global_lines if not l.startswith("# Memory Index")]
            if global_lines:
                parts.append("\n### Global Memories (shared across projects)")
                parts.append("\n".join(global_lines))

        result = "\n".join(parts)
        limit = max_lines if max_lines is not None else self._max_index_lines
        lines = result.splitlines()
        if len(lines) > limit:
            lines = lines[:limit]
        return "\n".join(lines)

    def record_access(self, name: str) -> bool:
        """先查项目层，再查全局层。"""
        if super().read_memory(name) is not None:
            return super().record_access(name)
        if self._global_store.read_memory(name) is not None:
            return self._global_store.record_access(name)
        return False

    def mark_stale_for_file(self, file_path: str) -> int:
        """两层都标记。"""
        count = super().mark_stale_for_file(file_path)
        count += self._global_store.mark_stale_for_file(file_path)
        return count

    def validate_memory(self, name: str) -> bool:
        """先查项目层，再查全局层。"""
        if super().read_memory(name) is not None:
            return super().validate_memory(name)
        if self._global_store.read_memory(name) is not None:
            return self._global_store.validate_memory(name)
        return False

    def prune_expired(
        self,
        max_episodic_age_days: int = 30,
        *,
        max_user_age_days: int | None = None,
    ) -> int:
        """两层都清理。"""
        count = super().prune_expired(
            max_episodic_age_days,
            max_user_age_days=max_user_age_days,
        )
        count += self._global_store.prune_expired(
            max_episodic_age_days,
            max_user_age_days=max_user_age_days,
        )
        return count

    def consolidate(self, candidate: Any, external_store: Any = None, backend: Any = None) -> str:
        """按候选类型路由到正确的层。"""
        memory = candidate.to_memory()
        if memory.metadata.type in _GLOBAL_MEMORY_TYPES:
            return self._global_store.consolidate(candidate, external_store, backend)
        return super().consolidate(candidate, external_store, backend)
