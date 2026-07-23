"""API integration tests for /api/memory endpoints."""
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from memory.store import MemoryStore
from memory.models import Memory, MemoryMetadata


def _make_app(db_path: str) -> FastAPI:
    """Create a minimal FastAPI app with a memory router and test service."""
    from server.routers.memory import create_memory_router

    # Create a mock service
    store = MemoryStore(repo_path=".", db_path=db_path)
    service = MagicMock()
    service._memory_store = store
    service._external_store = None

    app = FastAPI()
    app.state.service = service
    app.include_router(create_memory_router(lambda: service))
    return app


class TestMemoryAPI:
    db_path: str = ""

    @classmethod
    def setup_class(cls):
        cls.db_path = os.path.join(tempfile.gettempdir(), f"test_mem_api_{os.getpid()}.db")
        # Create tables
        with sqlite3.connect(cls.db_path) as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS memory_entries (
                    name TEXT PRIMARY KEY, description TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '', type TEXT NOT NULL DEFAULT 'project',
                    status TEXT NOT NULL DEFAULT 'active', scope TEXT NOT NULL DEFAULT 'project',
                    confidence REAL NOT NULL DEFAULT 0.7, access_count INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT '', source_session_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    expires_at TEXT
                );
                CREATE TABLE IF NOT EXISTS memory_anchors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, memory_name TEXT NOT NULL,
                    kind TEXT NOT NULL, path TEXT, symbol_name TEXT, task_value TEXT, content_hash TEXT
                );
            """)
        cls.app = _make_app(cls.db_path)
        cls.client = TestClient(cls.app)

    @classmethod
    def teardown_class(cls):
        try:
            os.unlink(cls.db_path)
        except OSError:
            pass

    def _clean(self):
        with sqlite3.connect(self.db_path) as c:
            c.execute("DELETE FROM memory_entries")
            c.execute("DELETE FROM memory_anchors")

    def test_1_list_empty(self):
        self._clean()
        resp = self.client.get("/api/memory")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["overview"]["total"] == 0

    def test_2_create(self):
        self._clean()
        resp = self.client.post("/api/memory", json={
            "name": "test-create", "description": "Test creation",
            "content": "# Hello", "type": "project",
        })
        assert resp.status_code == 201, resp.text
        assert resp.json()["status"] == "created"

    def test_3_create_duplicate(self):
        """Duplicate name creates a new version (upsert behavior)."""
        self._clean()
        self.client.post("/api/memory", json={
            "name": "dup", "description": "first", "content": "1",
        })
        resp = self.client.post("/api/memory", json={
            "name": "dup", "description": "second", "content": "2",
        })
        assert resp.status_code == 201, resp.text
        # Verify it was overwritten
        detail = self.client.get("/api/memory/dup").json()
        assert detail["description"] == "second"

    def test_4_list_with_items(self):
        self._clean()
        self.client.post("/api/memory", json={"name": "a", "description": "A", "content": "# A"})
        self.client.post("/api/memory", json={"name": "b", "description": "B", "content": "# B"})
        resp = self.client.get("/api/memory")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 2

    def test_5_get_detail(self):
        self._clean()
        self.client.post("/api/memory", json={
            "name": "detail-test", "description": "Detail test", "content": "## Detail\ncontent",
        })
        resp = self.client.get("/api/memory/detail-test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "detail-test"
        assert data["content"] == "## Detail\ncontent"
        assert "created_at" in data
        assert "updated_at" in data

    def test_6_get_detail_not_found(self):
        resp = self.client.get("/api/memory/nonexistent")
        assert resp.status_code == 404

    def test_7_update(self):
        self._clean()
        self.client.post("/api/memory", json={
            "name": "upd", "description": "before", "content": "original",
        })
        resp = self.client.patch("/api/memory/upd", json={
            "description": "after", "content": "updated",
        })
        assert resp.status_code == 200
        assert resp.json()["changed"] is True
        # Verify
        detail = self.client.get("/api/memory/upd").json()
        assert detail["description"] == "after"
        assert detail["content"] == "updated"

    def test_8_update_not_found(self):
        resp = self.client.patch("/api/memory/nonexistent", json={"description": "x"})
        assert resp.status_code == 404

    def test_9_delete(self):
        self._clean()
        self.client.post("/api/memory", json={
            "name": "del", "description": "to delete", "content": "bye",
        })
        resp = self.client.delete("/api/memory/del")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        # Verify gone
        resp2 = self.client.get("/api/memory/del")
        assert resp2.status_code == 404

    def test_10_delete_not_found(self):
        resp = self.client.delete("/api/memory/nonexistent")
        assert resp.status_code == 404

    def test_11_expand(self):
        self._clean()
        self.client.post("/api/memory", json={
            "name": "exp", "description": "expand test", "content": "## Expanded\ncontent",
        })
        resp = self.client.get("/api/memory?_expand=true")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert "content" in item
        assert "created_at" in item

    def test_12_stats(self):
        self._clean()
        resp = self.client.get("/api/memory/stats")
        assert resp.status_code == 200
        assert "total" in resp.json()
        assert "by_type" in resp.json()

    def test_13_search_q(self):
        self._clean()
        self.client.post("/api/memory", json={
            "name": "apple", "description": "A fruit", "content": "## Apple\nsweet",
        })
        self.client.post("/api/memory", json={
            "name": "banana", "description": "Another fruit", "content": "## Banana\nyellow",
        })
        # Search by name
        resp = self.client.get("/api/memory?q=apple")
        assert len(resp.json()["items"]) == 1
        assert resp.json()["items"][0]["name"] == "apple"
        # Search by content
        resp2 = self.client.get("/api/memory?q=yellow")
        assert len(resp2.json()["items"]) == 1
        assert resp2.json()["items"][0]["name"] == "banana"
