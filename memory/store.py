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
import re
from pathlib import Path
from typing import Any

import yaml

from memory.models import Memory, MemoryMetadata, MemorySummary

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_DEFAULT_BASE_DIR = "~/.forge-agent/projects"
_GLOBAL_MEMORY_DIR = "~/.forge-agent/global/memory"
_INDEX_FILENAME = "MEMORY.md"
_FRONTMATTER_SEP = "---"
_MAX_INDEX_LINES = 200  # MEMORY.md 默认最大行数

# user 和 feedback 类型默认存储到全局（跨项目共享）
_GLOBAL_MEMORY_TYPES = frozenset({"user", "feedback"})

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
    fm = {
        "name": memory.name,
        "description": memory.description,
        "metadata": {
            "type": memory.metadata.type,
        },
        "updated_at": memory.updated_at,
    }
    return yaml.dump(fm, default_flow_style=False, allow_unicode=True).strip()


def _build_memory_file(memory: Memory) -> str:
    """组装完整的记忆文件内容（frontmatter + body）。"""
    fm = _build_frontmatter(memory)
    return f"---\n{fm}\n---\n\n{memory.content.strip()}\n"


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
        self._ensure_dir()

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
        return Memory(
            name=fm.get("name", name),
            description=fm.get("description", ""),
            content=body,
            metadata=MemoryMetadata(
                type=meta.get("type", "reference"),
            ),
            updated_at=fm.get("updated_at", ""),
        )

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
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to write memory %s: %s", memory.name, exc)
            return False
        self._dirty = True
        if self._indexer is not None:
            try:
                self._indexer.index_memory(memory)
            except Exception as exc:
                logger.warning("Indexer failed for %s: %s", memory.name, exc)
        return True

    def list_memories(self) -> list[MemorySummary]:
        """
        列出所有记忆摘要。

        从 MEMORY.md 索引文件读取；索引不存在时扫描目录重建。

        Returns:
            MemorySummary 列表
        """
        if self._dirty or not self.index_path.exists():
            self._rebuild_index()
            self._dirty = False
        if self.index_path.exists():
            summaries = self._parse_index(self.index_path.read_text(encoding="utf-8"))
            if summaries:
                return summaries
        # 降级：扫描目录
        return self._scan_dir()

    def count_by_type(self) -> dict[str, int]:
        """
        统计每种类型的记忆数量。

        Returns:
            {type_name: count, ...} 例如 {"episodic": 3, "semantic": 5, "procedural": 2}
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
        if self._indexer is not None:
            try:
                self._indexer.remove_memory(name)
            except Exception as exc:
                logger.warning("Indexer remove failed for %s: %s", name, exc)
        return True

    # ------------------------------------------------------------------
    # 上下文注入
    # ------------------------------------------------------------------

    def get_index_content(self, max_lines: int | None = None) -> str:
        """
        获取 MEMORY.md 的内容（前 max_lines 行），用于注入 LLM 上下文。

        Args:
            max_lines: 最大行数，默认使用 self._max_index_lines

        Returns:
            MEMORY.md 的纯文本内容，空 store 返回空字符串
        """
        if self._dirty or not self.index_path.exists():
            # 索引脏了或不存在：重建
            self._rebuild_index()
            self._dirty = False
        if not self.index_path.exists():
            return ""

        text = self.index_path.read_text(encoding="utf-8").strip()
        # 索引只有标题行（没有记忆条目）时返回空
        if text.count("\n") == 0 and ("# Memory Index" in text or not text):
            return ""

        limit = max_lines if max_lines is not None else self._max_index_lines
        lines = text.splitlines()
        if len(lines) > limit:
            omitted = len(lines) - limit
            lines = lines[:limit]
            lines.append(f"... [{omitted} lines omitted]")
        return "\n".join(lines)

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
        """
        summaries = self._scan_dir()
        lines = ["# Memory Index\n"]
        for s in summaries:
            lines.append(
                f"- [{s.name}]({s.name}.md) — {s.description} ({s.type})"
            )
        self.index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

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
            summaries.append(MemorySummary(
                name=fm.get("name", fpath.stem),
                description=fm.get("description", ""),
                type=meta.get("type", "reference"),
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
                    type=m.group(4) or "reference",
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

        user/feedback → 全局层（跨项目共享）
        project/reference → 项目层
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
