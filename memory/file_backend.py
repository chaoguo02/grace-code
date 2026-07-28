"""
FileMemoryBackend — file-based memory storage.

Each memory is a .md file with YAML frontmatter, stored in:
    ~/.grace/projects/<hash>/memory/{name}.md
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from memory._utils import (
    atomic_write_text as _atomic_write_text,
    build_memory_file as _build_memory_file,
    needs_type_migration as _needs_type_migration,
    truncate_index as _truncate_index,
)
from memory.models import (
    Anchor, Memory, MemoryMetadata, MemoryScope, MemoryStatus,
    MemorySummary, MemoryType, parse_memory_type,
)
from utils.frontmatter import parse_frontmatter as _parse_frontmatter

logger = logging.getLogger(__name__)


class FileMemoryBackend:
    """File-based memory backend. Each memory is one .md file."""

    def __init__(
        self,
        store_dir: str | Path,
        max_index_lines: int = 200,
        indexer: Any | None = None,
    ) -> None:
        self._store_dir = Path(store_dir).expanduser().resolve()
        self._max_index_lines = max_index_lines
        self._indexer = indexer
        self._dirty = False
        self._anchor_index: dict[str, list[str]] | None = None
        self._access_count_cache: dict[str, int] = {}
        self._ensure_dir()
        # Lazy metadata cache
        from memory.metadata_cache import MetadataCache
        self._metadata_cache = MetadataCache()
        self._metadata_cache.build(self._store_dir)

    def _ensure_dir(self) -> None:
        self._store_dir.mkdir(parents=True, exist_ok=True)

    # Allowed characters for memory file names — alphanumeric, hyphen, underscore only.
    # Blocks path traversal attacks (CWE-22) by construction.
    _NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][-a-zA-Z0-9_]{0,127}$")

    def _file_path(self, name: str) -> Path:
        """Return the storage path for a memory file, validated against path traversal.

        Raises ValueError if *name* contains illegal characters or resolves
        outside the store directory.
        """
        if not self._NAME_PATTERN.match(name):
            raise ValueError(
                f"Invalid memory name: {name!r}. "
                f"Must be 1-128 alphanumeric characters, hyphens, or underscores."
            )
        return self._store_dir / f"{name}.md"

    @property
    def index_path(self) -> Path:
        return self._store_dir / "MEMORY.md"

    @property
    def archive_path(self) -> Path:
        return self._store_dir / "archive"

    # ── CRUD ────────────────────────────────────────────────────────────

    def read_memory(self, name: str) -> Memory | None:
        if not self._NAME_PATTERN.match(name):
            return None
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
        anchors = []
        for a in fm.get("anchors", []):
            if isinstance(a, dict):
                anchors.append(Anchor(kind=a.get("kind", "file"), path=a.get("path"),
                    name=a.get("name"), value=a.get("value"), content_hash=a.get("content_hash", "")))
        mem_meta = MemoryMetadata(
            type=parse_memory_type(fm, meta),
            status=MemoryStatus(meta.get("status", "active")) if isinstance(meta, dict) else MemoryStatus.ACTIVE,
            scope=MemoryScope(meta.get("scope", "project")) if isinstance(meta, dict) else MemoryScope.PROJECT,
            confidence=float(meta.get("confidence", 0.7)) if isinstance(meta, dict) else 0.7,
            importance=float(meta.get("importance", 0.5)) if isinstance(meta, dict) else 0.5,
            ttl_seconds=meta.get("ttl_seconds") if isinstance(meta, dict) else None,
            expires_at=meta.get("expires_at", "") if isinstance(meta, dict) else "",
            access_count=int(meta.get("access_count", 0)) if isinstance(meta, dict) else 0,
            validated_at=meta.get("validated_at", "") if isinstance(meta, dict) else "",
        )
        if _needs_type_migration(fm):
            memory = Memory(name=name, description=fm.get("description", ""),
                content=body, metadata=mem_meta, updated_at=fm.get("updated_at", ""), anchors=anchors)
            self.write_memory(memory)
        return Memory(name=name, description=fm.get("description", ""),
            content=body, metadata=mem_meta, updated_at=fm.get("updated_at", ""), anchors=anchors)

    def write_memory(self, memory: Memory, source: str = "", source_session_id: str = "", source_run_id: str = "") -> bool:
        _ = source; _ = source_session_id; _ = source_run_id
        content = _build_memory_file(memory)
        path = self._file_path(memory.name)
        # Defense-in-depth: verify the resolved path is still within the store
        try:
            _resolved = path.resolve()
        except OSError:
            _resolved = path.absolute()
        if not str(_resolved).startswith(str(self._store_dir.resolve())):
            raise ValueError(
                f"Memory path escapes store directory: {path}"
            )
        try:
            _atomic_write_text(path, content)
        except OSError as exc:
            logger.error("Failed to write memory %s: %s", memory.name, exc)
            return False
        self._dirty = True
        self._anchor_index = None
        cache = getattr(self, "_metadata_cache", None)
        if cache is not None:
            cache.upsert(memory)
        if self._indexer is not None:
            try: self._indexer.index_memory(memory)
            except Exception as exc: logger.warning("Indexer failed for %s: %s", memory.name, exc)
        return True

    def delete_memory(self, name: str) -> bool:
        path = self._file_path(name)
        if not path.exists():
            return True
        try:
            path.unlink()
        except OSError as exc:
            logger.error("Failed to delete memory %s: %s", name, exc)
            return False
        self._dirty = True
        self._anchor_index = None
        cache = getattr(self, "_metadata_cache", None)
        if cache is not None:
            cache.remove(name)
        if self._indexer is not None:
            try: self._indexer.remove_memory(name)
            except Exception as exc: logger.warning("Indexer remove failed for %s: %s", name, exc)
        return True

    def list_memories(self) -> list[MemorySummary]:
        cache = getattr(self, "_metadata_cache", None)
        if cache is not None and cache.is_built and cache.count > 0:
            return cache.list_summaries()
        if self._dirty or not self.index_path.exists():
            self._rebuild_index()
            self._dirty = False
        if self.index_path.exists():
            summaries = self._parse_index(self.index_path.read_text(encoding="utf-8"))
            if summaries:
                return summaries
        return self._scan_dir()

    def count_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for summary in self.list_memories():
            t = summary.type
            counts[t] = counts.get(t, 0) + 1
        return counts

    def list_by_scope(self, scope: str = "project", min_confidence: float = 0.0) -> list[Memory]:
        cache = getattr(self, "_metadata_cache", None)
        if cache is not None and cache.is_built and cache.count > 0:
            return cache.list_by_scope(scope, min_confidence)
        result: list[Memory] = []
        for s in self.list_memories():
            mem = self.read_memory(s.name)
            if mem is None: continue
            if mem.metadata.scope != scope: continue
            if mem.metadata.confidence < min_confidence: continue
            result.append(mem)
        return result

    def record_access(self, name: str) -> bool:
        mem = self.read_memory(name)
        if mem is None:
            return False
        mem.metadata.access_count += 1
        self.write_memory(mem)
        return True

    def get_index_content(self, max_lines: int | None = None) -> str:
        if self._dirty or not self.index_path.exists():
            self._rebuild_index()
            self._dirty = False
        if not self.index_path.exists():
            return ""
        text = self.index_path.read_text(encoding="utf-8")
        if max_lines or self._max_index_lines:
            return _truncate_index(text, max_lines or self._max_index_lines, 25_600)
        return text

    # ── Internal helpers (from original store.py) ────────────────────────

    def _rebuild_index(self) -> None:
        lines = ["# Memory Index\n"]
        for m in self._scan_dir():
            lines.append(f"- [{m.name}]({m.name}.md) -- {m.description} ({m.type})\n")
        content = "".join(lines)
        self.index_path.write_text(content, encoding="utf-8")

    def _scan_dir(self) -> list[MemorySummary]:
        result: list[MemorySummary] = []
        if not self._store_dir.is_dir():
            return result
        for path in sorted(self._store_dir.glob("*.md")):
            if path.name == "MEMORY.md":
                continue
            try:
                text = path.read_text(encoding="utf-8")
                fm, _ = _parse_frontmatter(text)
                meta = fm.get("metadata", {}) or {}
                result.append(MemorySummary(
                    name=fm.get("name", path.stem),
                    description=fm.get("description", ""),
                    type=meta.get("type", "project") if isinstance(meta, dict) else "project",
                    updated_at=fm.get("updated_at", ""),
                ))
            except Exception:
                continue
        return result

    @staticmethod
    def _parse_index(text: str) -> list[MemorySummary]:
        import re
        result: list[MemorySummary] = []
        for line in text.splitlines():
            m = re.match(r"- \[(.+?)\]\((.+?)\)\s*--\s*(.+?)(?:\s*\((\w+)\))?\s*$", line)
            if m:
                name = m.group(1)
                updated = ""
                result.append(MemorySummary(name=name, description=m.group(3).strip(),
                    type=m.group(4) or "project", updated_at=updated))
        return result
