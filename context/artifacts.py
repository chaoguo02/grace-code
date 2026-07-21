"""
context/artifacts.py

Artifact Store — 大型工具输出的外部化存储。

设计原理：
- 工具输出超过 token 阈值时，原始内容存入内存中的 ArtifactStore
- 对话历史中只保留短摘要引用（artifact_id + 首 N 行 + 统计信息）
- LLM 可通过 artifact_id 请求完整内容（未来扩展）
- 支持 LRU 淘汰，避免内存无限增长

与 Claude Code 的 artifact 思路一致：
- 大输出不塞进 prompt，保持 context window 精简
- 摘要保留足够信息让 LLM 决定是否需要完整内容

接入点：
- agent/core.py 的 _build_tool_result_content() 调用 maybe_store()
- 返回 (应放入历史的文本, 是否被artifact化)
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from context.token_budget import estimate_tokens

logger = logging.getLogger(__name__)


@dataclass
class Artifact:
    """存储的单个 artifact。"""
    artifact_id: str
    tool_name: str
    full_content: str
    summary: str
    token_count: int
    char_count: int
    line_count: int
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Artifact":
        return cls(
            artifact_id=str(data.get("artifact_id", "")),
            tool_name=str(data.get("tool_name", "")),
            full_content=str(data.get("full_content", "")),
            summary=str(data.get("summary", "")),
            token_count=int(data.get("token_count", 0)),
            char_count=int(data.get("char_count", 0)),
            line_count=int(data.get("line_count", 0)),
            created_at=float(data.get("created_at", time.time())),
        )

    def reference_text(self) -> str:
        """生成放入历史的引用文本。"""
        return (
            f"[Artifact {self.artifact_id} | {self.tool_name} | "
            f"{self.line_count} lines, ~{self.token_count} tokens]\n"
            f"{self.summary}"
        )


class ArtifactStore:
    """
    内存中的 artifact 存储。LRU 淘汰策略。

    用法：
        store = ArtifactStore(threshold_tokens=2000, max_artifacts=50)
        text_for_history, was_stored = store.maybe_store(tool_name, output)
    """

    def __init__(
        self,
        threshold_tokens: int = 2000,
        max_artifacts: int = 50,
        summary_lines: int = 15,
        summary_tail_lines: int = 5,
        storage_dir: str | Path | None = None,
        max_total_bytes: int = 10_000_000,
        max_content_bytes: int = 1_000_000,
    ) -> None:
        self._threshold_tokens = threshold_tokens
        self._max_artifacts = max_artifacts
        self._summary_lines = summary_lines
        self._summary_tail_lines = summary_tail_lines
        self._max_total_bytes = max_total_bytes
        self._max_content_bytes = max_content_bytes
        self._total_bytes: int = 0
        self._store: OrderedDict[str, Artifact] = OrderedDict()
        self._storage_dir: Path | None = None
        self._evicted_ids: set[str] = set()
        if storage_dir is not None:
            self.set_storage_dir(storage_dir)

    @property
    def threshold_tokens(self) -> int:
        return self._threshold_tokens

    def set_storage_dir(self, storage_dir: str | Path) -> None:
        """Attach durable storage and load existing artifacts."""
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._load_from_disk()

    def store(self, tool_name: str, output: str) -> Artifact | None:
        """Store output as an artifact regardless of threshold."""
        if not output:
            return None
        token_count = estimate_tokens(output)
        artifact = self._create_artifact(tool_name, output, token_count)
        self._add(artifact)
        return artifact

    def maybe_store(self, tool_name: str, output: str) -> tuple[str, bool]:
        """
        检查输出是否需要 artifact 化。

        Args:
            tool_name: 产生此输出的工具名
            output: 工具原始输出

        Returns:
            (text_for_history, was_artifacted)
            - was_artifacted=False: 返回原始 output，不做处理
            - was_artifacted=True: 返回摘要引用文本
        """
        if not output:
            return output, False

        token_count = estimate_tokens(output)
        if token_count <= self._threshold_tokens:
            return output, False

        artifact = self._create_artifact(tool_name, output, token_count)
        self._add(artifact)
        return artifact.reference_text(), True

    def get(self, artifact_id: str) -> Artifact | None:
        """按 ID 获取 artifact，LRU 更新访问顺序。"""
        if artifact_id not in self._store:
            return None
        self._store.move_to_end(artifact_id)
        return self._store[artifact_id]

    def get_full_content(self, artifact_id: str) -> str | None:
        """获取 artifact 的完整内容。"""
        art = self.get(artifact_id)
        return art.full_content if art else None

    def list_artifacts(self) -> list[tuple[str, str, int]]:
        """返回 [(artifact_id, tool_name, token_count), ...]"""
        return [
            (art.artifact_id, art.tool_name, art.token_count)
            for art in self._store.values()
        ]

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[Artifact]:
        """Search artifacts by id, tool name, summary, or full content."""
        needle = (query or "").strip().lower()
        if not needle:
            return list(self._store.values())[: max(1, limit)]
        matches: list[Artifact] = []
        for artifact in self._store.values():
            haystack = "\n".join(
                [
                    artifact.artifact_id,
                    artifact.tool_name,
                    artifact.summary,
                    artifact.full_content,
                ]
            ).lower()
            if needle in haystack:
                matches.append(artifact)
            if len(matches) >= max(1, limit):
                break
        return matches

    @property
    def count(self) -> int:
        return len(self._store)

    @property
    def total_tokens_stored(self) -> int:
        return sum(a.token_count for a in self._store.values())

    def _create_artifact(self, tool_name: str, output: str, token_count: int) -> Artifact:
        """从原始输出创建 Artifact，生成摘要。"""
        lines = output.splitlines()
        line_count = len(lines)

        summary = self._build_summary(lines, tool_name, token_count, line_count)

        content_hash = hashlib.sha256(output[:1000].encode(errors="replace")).hexdigest()[:8]
        artifact_id = f"art_{content_hash}"

        # Cap per-artifact content at 1 MB to prevent OOM from single large output
        capped_output = output[:self._max_content_bytes]
        return Artifact(
            artifact_id=artifact_id,
            tool_name=tool_name,
            full_content=capped_output,
            summary=summary,
            token_count=token_count,
            char_count=len(capped_output),
            line_count=line_count,
        )

    def _build_summary(
        self, lines: list[str], tool_name: str, token_count: int, line_count: int
    ) -> str:
        """构建 artifact 摘要：保留首 N 行 + 尾 M 行 + 统计信息。"""
        head_n = self._summary_lines
        tail_n = self._summary_tail_lines
        max_summary_chars = 2000

        if line_count <= head_n + tail_n:
            joined = "\n".join(lines)
            if len(joined) <= max_summary_chars:
                return joined
            # Few lines but very long — char-level truncation
            return (
                joined[:max_summary_chars // 2]
                + f"\n... [{len(joined) - max_summary_chars} chars omitted, ~{token_count} tokens total] ...\n"
                + joined[-max_summary_chars // 4:]
            )

        head = lines[:head_n]
        tail = lines[-tail_n:] if tail_n > 0 else []
        omitted = line_count - head_n - tail_n

        parts = []
        parts.extend(head)
        parts.append(f"... [{omitted} lines omitted, ~{token_count} tokens total] ...")
        if tail:
            parts.extend(tail)

        return "\n".join(parts)

    def _add(self, artifact: Artifact) -> None:
        """添加 artifact，执行 LRU 淘汰（数量 + 内存双重限制）。"""
        content_len = len(artifact.full_content)
        if artifact.artifact_id in self._store:
            self._total_bytes -= len(self._store[artifact.artifact_id].full_content)
            self._store.move_to_end(artifact.artifact_id)
            self._store[artifact.artifact_id] = artifact
            self._total_bytes += content_len
        else:
            self._store[artifact.artifact_id] = artifact
            self._total_bytes += content_len

        self._persist_artifact(artifact)

        # Limit 1: count-based LRU
        evicted = 0
        while len(self._store) > self._max_artifacts:
            removed_id, removed = self._store.popitem(last=False)
            self._total_bytes -= len(removed.full_content)
            self._evicted_ids.add(removed_id)
            self._delete_artifact_file(removed_id)
            evicted += 1
        # Limit 2: total bytes (10 MB default)
        while self._total_bytes > self._max_total_bytes and self._store:
            removed_id, removed = self._store.popitem(last=False)
            self._total_bytes -= len(removed.full_content)
            self._evicted_ids.add(removed_id)
            self._delete_artifact_file(removed_id)
            evicted += 1
        if evicted:
            evicted_ids = ",".join(list(self._evicted_ids)[-3:])
            logger.debug("ArtifactStore evicted %d artifacts (total_bytes=%d, evicted=%s)",
                         evicted, self._total_bytes, evicted_ids)

    def _artifact_path(self, artifact_id: str) -> Path | None:
        if self._storage_dir is None:
            return None
        safe_id = "".join(ch for ch in artifact_id if ch.isalnum() or ch in {"_", "-"})
        return self._storage_dir / f"{safe_id}.json"

    def _persist_artifact(self, artifact: Artifact) -> None:
        path = self._artifact_path(artifact.artifact_id)
        if path is None:
            return
        path.write_text(
            json.dumps(artifact.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _delete_artifact_file(self, artifact_id: str) -> None:
        path = self._artifact_path(artifact_id)
        if path is None or not path.exists():
            return
        try:
            path.unlink()
        except OSError:
            pass

    def _load_from_disk(self) -> None:
        if self._storage_dir is None or not self._storage_dir.exists():
            return
        for path in sorted(self._storage_dir.glob("art_*.json")):
            try:
                artifact = Artifact.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
            if artifact.artifact_id:
                self._store[artifact.artifact_id] = artifact
        while len(self._store) > self._max_artifacts:
            self._store.popitem(last=False)
