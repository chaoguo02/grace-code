"""
memory/external_store.py

外部记忆存储：SQLite + 向量语义搜索。

和 MemoryStore（文件型、按名精确查找）互补：
- MemoryStore: 本地文件，按 name 读取，索引常驻 context
- ExternalMemoryStore: SQLite 持久化，语义搜索，适合"我记得有件事但想不起来叫什么"

用法：
    store = ExternalMemoryStore("~/.forge-agent/external_memory.db")
    store.add_memory("fix-bug-123", "The login bug was caused by a missing null check in auth.py line 42.")
    results = store.search("login authentication problem")
    # → [{"name": "fix-bug-123", "score": 0.87, "content": "..."}, ...]
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Embedding 引擎（基于 fastembed，不依赖 PyTorch）
# ---------------------------------------------------------------------------

_EMBEDDING_MODEL = None
_EMBEDDING_LOCK = threading.Lock()
_DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"  # 中英文通用，轻量（33MB）


def _get_embedding_model(model_name: str = _DEFAULT_MODEL):
    """延迟加载 embedding 模型。"""
    global _EMBEDDING_MODEL
    if _EMBEDDING_MODEL is None:
        with _EMBEDDING_LOCK:
            if _EMBEDDING_MODEL is None:
                try:
                    from fastembed import TextEmbedding
                except ImportError:
                    raise ImportError(
                        "fastembed is required for semantic search. "
                        "Install: pip install fastembed"
                    )
                # 使用项目本地缓存，首次运行从 HF 镜像自动下载
                project_cache = Path(__file__).resolve().parent.parent / ".cache" / "fastembed"
                project_cache.mkdir(parents=True, exist_ok=True)
                if not os.environ.get("HF_ENDPOINT"):
                    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
                logger.info("Loading embedding model %s...", model_name)
                _EMBEDDING_MODEL = TextEmbedding(
                    model_name=model_name,
                    cache_dir=str(project_cache),
                )
                logger.info("Embedding model loaded.")
    return _EMBEDDING_MODEL


def _encode(text: str, model_name: str = _DEFAULT_MODEL) -> np.ndarray:
    """将文本编码为 embedding 向量。"""
    model = _get_embedding_model(model_name)
    embeddings = list(model.embed([text]))
    if not embeddings:
        raise RuntimeError(f"Embedding failed for: {text[:50]}")
    vec = embeddings[0]
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def _encode_batch(texts: list[str], model_name: str = _DEFAULT_MODEL) -> list[np.ndarray]:
    """批量编码多段文本为 embedding 向量（一次 model 调用，比逐条快）。"""
    if not texts:
        return []
    model = _get_embedding_model(model_name)
    embeddings = list(model.embed(texts))
    result = []
    for vec in embeddings:
        norm = np.linalg.norm(vec)
        result.append(vec / norm if norm > 0 else vec)
    return result


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """计算余弦相似度（向量已归一化时等价于点积）。"""
    return float(np.dot(a, b))


def _embedding_to_bytes(emb: np.ndarray) -> bytes:
    """numpy float32 array → bytes（SQLite BLOB）。"""
    return emb.astype(np.float32).tobytes()


def _bytes_to_embedding(data: bytes) -> np.ndarray:
    """bytes → numpy float32 array。"""
    return np.frombuffer(data, dtype=np.float32)


# ---------------------------------------------------------------------------
# ExternalMemoryStore
# ---------------------------------------------------------------------------

class ExternalMemoryStore:
    """
    SQLite + 向量语义搜索的外部记忆存储。

    Args:
        db_path: SQLite 数据库路径（默认 ~/.forge-agent/external_memory.db）
        model_name: sentence-transformers 模型名
    """

    def __init__(
        self,
        db_path: str | None = None,
        model_name: str = _DEFAULT_MODEL,
    ) -> None:
        if db_path is None:
            db_path = str(Path.home() / ".forge-agent" / "external_memory.db")
        self._db_path = str(Path(db_path).expanduser().resolve())
        self._model_name = model_name
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def db_path(self) -> str:
        return self._db_path

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add_memory(
        self,
        name: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """
        写入一条记忆，自动生成 embedding。

        Args:
            name: 记忆名称（slug，唯一）
            content: 记忆正文
            metadata: 可选元数据

        Returns:
            True 表示成功
        """
        now = _now()
        embedding = _encode(content, self._model_name)
        emb_bytes = _embedding_to_bytes(embedding)
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)

        try:
            conn = self._get_conn()
            conn.execute(
                """INSERT OR REPLACE INTO memories
                   (name, content, embedding, metadata, created_at, updated_at)
                   VALUES (?, ?, ?, ?, COALESCE(
                       (SELECT created_at FROM memories WHERE name = ?), ?
                   ), ?)""",
                (name, content, emb_bytes, meta_json, name, now, now),
            )
            conn.commit()
            return True
        except Exception as exc:
            logger.error("Failed to write memory %s: %s", name, exc)
            return False

    def get_memory(self, name: str) -> dict[str, Any] | None:
        """
        按 name 读取记忆。

        Returns:
            {"name": ..., "content": ..., "metadata": ..., "created_at": ..., "updated_at": ...}
            不存在时返回 None
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT name, content, metadata, created_at, updated_at FROM memories WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return None
        return {
            "name": row[0],
            "content": row[1],
            "metadata": json.loads(row[2]) if row[2] else {},
            "created_at": row[3],
            "updated_at": row[4],
        }

    def search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[dict[str, Any]]:
        """
        语义搜索记忆。

        Args:
            query: 搜索关键词（自然语言）
            top_k: 返回前 N 条
            min_score: 最低相关度阈值 (0~1)

        Returns:
            [{"name": ..., "content": ..., "metadata": ..., "score": ..., "updated_at": ...}, ...]
            按 score 从高到低排序
        """
        if not query.strip():
            return []

        conn = self._get_conn()
        rows = conn.execute(
            "SELECT name, content, embedding, metadata, updated_at FROM memories",
        ).fetchall()

        if not rows:
            return []

        # 编码 query
        query_emb = _encode(query, self._model_name)

        # 计算相似度
        scored: list[tuple[float, dict[str, Any]]] = []
        for name, content, emb_bytes, meta_json, updated_at in rows:
            if not emb_bytes:
                continue
            emb = _bytes_to_embedding(emb_bytes)
            score = _cosine_similarity(query_emb, emb)
            if score < min_score:
                continue
            scored.append((
                score,
                {
                    "name": name,
                    "content": content,
                    "metadata": json.loads(meta_json) if meta_json else {},
                    "score": round(score, 4),
                    "updated_at": updated_at,
                },
            ))

        # 按得分排序
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored[:top_k]]

    def delete_memory(self, name: str) -> bool:
        """
        删除一条记忆。

        Returns:
            True 表示成功（不存在也返回 True）
        """
        try:
            conn = self._get_conn()
            conn.execute("DELETE FROM memories WHERE name = ?", (name,))
            conn.commit()
            return True
        except Exception as exc:
            logger.error("Failed to delete memory %s: %s", name, exc)
            return False

    def list_memories(self) -> list[dict[str, Any]]:
        """
        列出所有记忆（不含 embedding）。

        Returns:
            [{"name": ..., "content": ..., "metadata": ..., "created_at": ..., "updated_at": ...}, ...]
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT name, content, metadata, created_at, updated_at FROM memories ORDER BY updated_at DESC",
        ).fetchall()
        return [
            {
                "name": row[0],
                "content": row[1],
                "metadata": json.loads(row[2]) if row[2] else {},
                "created_at": row[3],
                "updated_at": row[4],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Chunk-level RAG API
    # ------------------------------------------------------------------

    def add_chunks(
        self,
        source_name: str,
        chunks: list[tuple[int, str, bytes, str]],
    ) -> bool:
        """
        批量写入 chunks（事务内先删旧再插新）。

        Args:
            source_name: 父记忆名
            chunks: [(chunk_index, content, embedding_bytes, metadata_json), ...]
        """
        now = _now()
        try:
            conn = self._get_conn()
            conn.execute(
                "DELETE FROM memory_chunks WHERE source_name = ?",
                (source_name,),
            )
            conn.executemany(
                """INSERT INTO memory_chunks
                   (source_name, chunk_index, content, embedding, metadata, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (source_name, idx, content, emb_bytes, meta_json, now, now)
                    for idx, content, emb_bytes, meta_json in chunks
                ],
            )
            conn.commit()
            return True
        except Exception as exc:
            logger.error("Failed to add chunks for %s: %s", source_name, exc)
            return False

    def delete_chunks(self, source_name: str) -> bool:
        """删除某条记忆的所有 chunks。"""
        try:
            conn = self._get_conn()
            conn.execute(
                "DELETE FROM memory_chunks WHERE source_name = ?",
                (source_name,),
            )
            conn.commit()
            return True
        except Exception as exc:
            logger.error("Failed to delete chunks for %s: %s", source_name, exc)
            return False

    def search_chunks(
        self,
        query: str,
        top_k: int = 10,
        min_score: float = 0.3,
        max_per_source: int = 2,
    ) -> list[dict[str, Any]]:
        """
        语义搜索 chunk 级别（比 memory 级别更精准）。

        混合评分 = cosine_sim * 0.85 + recency_score * 0.1 + type_boost * 0.05

        Args:
            query: 搜索文本
            top_k: 返回前 N 个 chunks
            min_score: 最低相似度阈值
            max_per_source: 每个 source_name 最多返回几个 chunk

        Returns:
            [{"source_name", "chunk_index", "content", "score", "metadata", "updated_at"}, ...]
        """
        if not query.strip():
            return []

        conn = self._get_conn()
        rows = conn.execute(
            "SELECT source_name, chunk_index, content, embedding, metadata, "
            "updated_at, access_count FROM memory_chunks",
        ).fetchall()

        if not rows:
            return []

        # Fast-path guard: max possible boosted score = cosine*0.85 + 0.15
        min_cosine = max(0.0, (min_score - 0.15) / 0.85)

        query_emb = _encode(query, self._model_name)
        now_ts = datetime.now(timezone.utc)

        type_boosts = {"feedback": 0.05, "user": 0.03}

        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            source_name = row[0]
            chunk_index = row[1]
            content = row[2]
            emb_bytes = row[3]
            meta_json = row[4]
            updated_at = row[5]
            access_count = row[6] or 0

            if not emb_bytes:
                continue

            emb = _bytes_to_embedding(emb_bytes)
            cosine = _cosine_similarity(query_emb, emb)

            # Quick-reject: even max boost can't save this cosine
            if cosine < min_cosine:
                continue

            if cosine < min_score:
                continue

            # Recency score: 越新越高
            try:
                updated_dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                days_ago = (now_ts - updated_dt).days
            except (ValueError, TypeError):
                days_ago = 30
            recency = 1.0 / (1.0 + days_ago)

            # Type boost
            meta = json.loads(meta_json) if meta_json else {}
            mem_type = meta.get("type", "")
            boost = type_boosts.get(mem_type, 0.0)

            final_score = cosine * 0.85 + recency * 0.1 + boost

            scored.append((final_score, {
                "source_name": source_name,
                "chunk_index": chunk_index,
                "content": content,
                "score": round(final_score, 4),
                "cosine": round(cosine, 4),
                "metadata": meta,
                "updated_at": updated_at,
            }))

        scored.sort(key=lambda x: x[0], reverse=True)

        # 每个 source 最多 max_per_source 个
        result: list[dict[str, Any]] = []
        source_counts: dict[str, int] = {}
        for _, item in scored:
            sn = item["source_name"]
            if source_counts.get(sn, 0) >= max_per_source:
                continue
            source_counts[sn] = source_counts.get(sn, 0) + 1
            result.append(item)
            if len(result) >= top_k:
                break

        # 更新 access_count
        if result:
            try:
                conn.executemany(
                    "UPDATE memory_chunks SET access_count = access_count + 1 "
                    "WHERE source_name = ? AND chunk_index = ?",
                    [(r["source_name"], r["chunk_index"]) for r in result],
                )
                conn.commit()
            except Exception:
                pass

        return result

    def close(self) -> None:
        """关闭数据库连接。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._create_conn()
        return self._conn

    def _create_conn(self) -> sqlite3.Connection:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")  # 并发读写安全
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """建表（幂等）。"""
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                content TEXT NOT NULL,
                embedding BLOB,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_name
            ON memories(name)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_name TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                embedding BLOB,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                access_count INTEGER DEFAULT 0,
                UNIQUE(source_name, chunk_index)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_source
            ON memory_chunks(source_name)
        """)
        conn.commit()

    def __enter__(self) -> "ExternalMemoryStore":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"ExternalMemoryStore(db={self._db_path})"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
