"""
memory/indexer.py

MemoryIndexer — 将 MemoryStore 的写入/删除自动同步到向量索引。

每次 write_memory 后自动调用 index_memory：
  1. 分块 (chunker)
  2. 批量 embed
  3. 写入 SQLite memory_chunks 表

删除时清理对应 chunks。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from memory.chunker import chunk_memory
from memory.external_store import (
    _encode_batch,
    _embedding_to_bytes,
    _DEFAULT_MODEL,
)

if TYPE_CHECKING:
    from memory.external_store import ExternalMemoryStore
    from memory.models import Memory
    from memory.store import MemoryStore

logger = logging.getLogger(__name__)


class MemoryIndexer:
    """
    自动向量索引器。

    由 MemoryStore 在 write/delete 时调用，保证 SQLite chunks 与文件记忆同步。
    """

    def __init__(
        self,
        external_store: ExternalMemoryStore,
        model_name: str = _DEFAULT_MODEL,
    ) -> None:
        self._external = external_store
        self._model_name = model_name

    def index_memory(self, memory: Memory) -> bool:
        """
        对一条记忆进行分块 + embed + 写入 chunks 表。

        先删除旧 chunks（如有），再写入新的。
        """
        try:
            chunks = chunk_memory(
                name=memory.name,
                description=memory.description,
                content=memory.content,
                metadata={"type": memory.metadata.type},
            )

            embed_texts = [c.embed_text for c in chunks]
            embeddings = _encode_batch(embed_texts, self._model_name)

            rows: list[tuple[int, str, bytes, str]] = []
            for chunk, emb in zip(chunks, embeddings):
                meta_json = json.dumps(chunk.metadata, ensure_ascii=False)
                rows.append((
                    chunk.chunk_index,
                    chunk.content,
                    _embedding_to_bytes(emb),
                    meta_json,
                ))

            return self._external.add_chunks(memory.name, rows)

        except Exception as exc:
            logger.error("Failed to index memory %s: %s", memory.name, exc)
            return False

    def remove_memory(self, name: str) -> bool:
        """删除某条记忆的所有 chunks。"""
        return self._external.delete_chunks(name)

    def reindex_all(self, store: MemoryStore) -> int:
        """
        全量重建索引（首次迁移或修复用）。

        Returns:
            成功索引的记忆条数
        """
        summaries = store.list_memories()
        count = 0
        for summary in summaries:
            memory = store.read_memory(summary.name)
            if memory and self.index_memory(memory):
                count += 1
        logger.info("Reindexed %d/%d memories", count, len(summaries))
        return count
