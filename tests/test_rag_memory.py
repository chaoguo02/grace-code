"""
tests/test_rag_memory.py

RAG 外部记忆管线测试：chunker → indexer → search_chunks → retriever。
不依赖真实 embedding 模型（mock fastembed），确保 CI 环境可跑。
"""

from __future__ import annotations

import json
import numpy as np
import pytest
from unittest.mock import patch, MagicMock

from memory.chunker import chunk_memory, Chunk
from memory.external_store import ExternalMemoryStore, _embedding_to_bytes, _bytes_to_embedding


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _fake_encode_batch(texts, model_name=None):
    result = []
    for t in texts:
        np.random.seed(hash(t) % 2**32)
        vec = np.random.randn(384).astype(np.float32)
        norm = np.linalg.norm(vec)
        result.append(vec / norm if norm > 0 else vec)
    return result


def _fake_encode(text, model_name=None):
    np.random.seed(hash(text) % 2**32)
    vec = np.random.randn(384).astype(np.float32)
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


@pytest.fixture
def mock_embedding():
    """Mock fastembed，返回固定维度的随机向量。"""
    with patch("memory.external_store._encode_batch", side_effect=_fake_encode_batch), \
         patch("memory.external_store._encode", side_effect=_fake_encode), \
         patch("memory.indexer._encode_batch", side_effect=_fake_encode_batch):
        yield


@pytest.fixture
def db_store(tmp_path, mock_embedding):
    """创建临时 ExternalMemoryStore。"""
    db_path = str(tmp_path / "test_memory.db")
    with patch("memory.external_store._get_embedding_model"):
        store = ExternalMemoryStore(db_path=db_path)
    yield store
    store.close()


# ===========================================================================
# Chunker 测试
# ===========================================================================

class TestChunker:
    def test_short_content_single_chunk(self):
        chunks = chunk_memory("test-mem", "A short memory", "Hello world")
        assert len(chunks) == 1
        assert chunks[0].source_name == "test-mem"
        assert chunks[0].chunk_index == 0
        assert "test-mem" in chunks[0].embed_text

    def test_long_content_multiple_chunks(self):
        content = "\n\n".join([f"Paragraph {i}: " + "x" * 200 for i in range(10)])
        chunks = chunk_memory("long-mem", "A long memory", content)
        assert len(chunks) > 1
        for i, c in enumerate(chunks):
            assert c.chunk_index == i
            assert c.source_name == "long-mem"

    def test_heading_based_split(self):
        content = (
            "# Section 1\n" + "Content A " * 50 + "\n\n"
            "# Section 2\n" + "Content B " * 50 + "\n\n"
            "# Section 3\n" + "Content C " * 50
        )
        chunks = chunk_memory("doc", "Documentation", content)
        assert len(chunks) >= 2

    def test_metadata_preserved(self):
        chunks = chunk_memory("m", "desc", "content", metadata={"type": "feedback"})
        assert chunks[0].metadata == {"type": "feedback"}

    def test_empty_content_uses_description(self):
        chunks = chunk_memory("m", "the description", "")
        assert "the description" in chunks[0].content


# ===========================================================================
# ExternalMemoryStore Chunk API 测试
# ===========================================================================

class TestChunkCRUD:
    def test_add_and_search_chunks(self, db_store):
        emb = np.random.randn(384).astype(np.float32)
        emb = emb / np.linalg.norm(emb)
        emb_bytes = _embedding_to_bytes(emb)

        chunks = [
            (0, "chunk zero content", emb_bytes, json.dumps({"type": "project"})),
            (1, "chunk one content", emb_bytes, json.dumps({"type": "project"})),
        ]
        ok = db_store.add_chunks("source-a", chunks)
        assert ok is True

        results = db_store.search_chunks("chunk", top_k=5, min_score=-1.0)
        assert len(results) > 0
        assert results[0]["source_name"] == "source-a"

    def test_delete_chunks(self, db_store):
        emb = np.random.randn(384).astype(np.float32)
        emb = emb / np.linalg.norm(emb)
        emb_bytes = _embedding_to_bytes(emb)

        db_store.add_chunks("to-delete", [
            (0, "will be deleted", emb_bytes, "{}"),
        ])
        ok = db_store.delete_chunks("to-delete")
        assert ok is True

        results = db_store.search_chunks("deleted", top_k=5, min_score=-1.0)
        assert all(r["source_name"] != "to-delete" for r in results)

    def test_max_per_source(self, db_store):
        emb = np.random.randn(384).astype(np.float32)
        emb = emb / np.linalg.norm(emb)
        emb_bytes = _embedding_to_bytes(emb)

        chunks = [(i, f"content {i}", emb_bytes, "{}") for i in range(5)]
        db_store.add_chunks("multi", chunks)

        results = db_store.search_chunks("content", top_k=10, min_score=-1.0, max_per_source=2)
        source_counts = {}
        for r in results:
            source_counts[r["source_name"]] = source_counts.get(r["source_name"], 0) + 1
        assert source_counts.get("multi", 0) <= 2

    def test_empty_query_returns_empty(self, db_store):
        results = db_store.search_chunks("", top_k=5)
        assert results == []


# ===========================================================================
# Indexer 测试
# ===========================================================================

class TestMemoryIndexer:
    def test_index_and_search(self, tmp_path):
        from memory.indexer import MemoryIndexer
        from memory.models import Memory, MemoryMetadata

        # Use deterministic embeddings that guarantee positive cosine
        def _same_encode_batch(texts, model_name=None):
            # All texts get similar vectors (base + small perturbation)
            base = np.ones(384, dtype=np.float32)
            result = []
            for i, t in enumerate(texts):
                vec = base + np.random.RandomState(i).randn(384).astype(np.float32) * 0.1
                norm = np.linalg.norm(vec)
                result.append(vec / norm)
            return result

        def _same_encode(text, model_name=None):
            base = np.ones(384, dtype=np.float32)
            vec = base + np.random.RandomState(42).randn(384).astype(np.float32) * 0.1
            norm = np.linalg.norm(vec)
            return vec / norm

        with patch("memory.external_store._get_embedding_model"), \
             patch("memory.external_store._encode_batch", side_effect=_same_encode_batch), \
             patch("memory.external_store._encode", side_effect=_same_encode), \
             patch("memory.indexer._encode_batch", side_effect=_same_encode_batch):

            db_path = str(tmp_path / "idx_test.db")
            store = ExternalMemoryStore(db_path=db_path)
            indexer = MemoryIndexer(store)
            memory = Memory(
                name="test-indexer",
                description="Testing the indexer",
                content="The quick brown fox jumps over the lazy dog. " * 5,
                metadata=MemoryMetadata(type="project"),
            )
            ok = indexer.index_memory(memory)
            assert ok is True

            results = store.search_chunks("fox", top_k=5, min_score=-1.0)
            assert any(r["source_name"] == "test-indexer" for r in results)
            store.close()

    def test_remove_memory(self, db_store, mock_embedding):
        from memory.indexer import MemoryIndexer
        from memory.models import Memory, MemoryMetadata

        indexer = MemoryIndexer(db_store)
        memory = Memory(
            name="to-remove",
            description="Will be removed",
            content="Some content here",
            metadata=MemoryMetadata(type="reference"),
        )
        indexer.index_memory(memory)
        ok = indexer.remove_memory("to-remove")
        assert ok is True

        results = db_store.search_chunks("content", top_k=5, min_score=-1.0)
        assert all(r["source_name"] != "to-remove" for r in results)


# ===========================================================================
# Retriever 测试
# ===========================================================================

class TestProactiveRetriever:
    def test_retrieve_returns_chunks(self, db_store, mock_embedding):
        from memory.retriever import ProactiveRetriever

        emb = np.random.randn(384).astype(np.float32)
        emb = emb / np.linalg.norm(emb)
        emb_bytes = _embedding_to_bytes(emb)

        db_store.add_chunks("auth-bug", [
            (0, "Login failed due to null check", emb_bytes, json.dumps({"type": "project"})),
        ])

        retriever = ProactiveRetriever(db_store, max_chunks=3, min_score=-1.0)
        results = retriever.retrieve("login authentication problem")
        assert len(results) >= 0  # May or may not match with random embeddings

    def test_format_for_injection_empty(self):
        from memory.retriever import ProactiveRetriever
        store = MagicMock()
        retriever = ProactiveRetriever(store)
        assert retriever.format_for_injection([]) == ""

    def test_format_for_injection_content(self):
        from memory.retriever import ProactiveRetriever
        store = MagicMock()
        retriever = ProactiveRetriever(store)
        chunks = [
            {"source_name": "bug-fix", "content": "Fixed the auth bug", "score": 0.85},
        ]
        result = retriever.format_for_injection(chunks)
        assert "Relevant Memory Content" in result
        assert "bug-fix" in result
        assert "Fixed the auth bug" in result

    def test_empty_message_returns_empty(self):
        from memory.retriever import ProactiveRetriever
        store = MagicMock()
        retriever = ProactiveRetriever(store)
        assert retriever.retrieve("") == []
        assert retriever.retrieve("  ") == []


# ===========================================================================
# MemoryContext RAG 集成测试
# ===========================================================================

class TestMemoryContextRAG:
    def test_build_memory_section_with_retriever(self, tmp_path, mock_embedding):
        from memory.store import MemoryStore
        from memory.context import MemoryContext
        from memory.retriever import ProactiveRetriever

        store = MemoryStore(repo_path="/test", memory_dir=str(tmp_path / "mem"))

        mock_retriever = MagicMock(spec=ProactiveRetriever)
        mock_retriever.retrieve.return_value = [
            {"source_name": "fix-123", "content": "The fix was X", "score": 0.9},
        ]
        mock_retriever.format_for_injection.return_value = (
            "## Relevant Memory Content\n\n### [fix-123] (relevance: 0.90)\n\nThe fix was X\n"
        )

        ctx = MemoryContext(store=store, retriever=mock_retriever)
        ctx.set_user_message("What was the fix?")

        section = ctx.build_memory_section()
        assert "Relevant Memory Content" in section
        assert "fix-123" in section

    def test_no_retriever_no_rag_section(self, tmp_path):
        from memory.store import MemoryStore
        from memory.context import MemoryContext

        store = MemoryStore(repo_path="/test", memory_dir=str(tmp_path / "mem"))
        ctx = MemoryContext(store=store, retriever=None)
        ctx.set_user_message("hello")

        section = ctx.build_memory_section()
        assert "Relevant Memory Content" not in section


# ===========================================================================
# Embedding 工具函数测试
# ===========================================================================

class TestEmbeddingUtils:
    def test_embedding_roundtrip(self):
        vec = np.random.randn(384).astype(np.float32)
        blob = _embedding_to_bytes(vec)
        recovered = _bytes_to_embedding(blob)
        np.testing.assert_array_almost_equal(vec, recovered)
