"""
Memory router — CRUD for long-term memory, backed by SQLite.

Mounted under ``/api/memory``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server.schemas.memory import (
    MemoryCreateRequest, MemoryDetailResponse, MemoryListResponse,
    MemoryItemResponse, MemoryUpdateRequest,
)

logger = logging.getLogger(__name__)


class RecallPreviewRequest(BaseModel):
    query: str = Field(default="")
    top_k: int = Field(default=8, ge=1, le=20)


class MemoryOverrideRequest(BaseModel):
    memory_name: str = Field(min_length=1)
    action: str = Field(description="pin | disable | unpin | enable")


class MemoryEdgeRequest(BaseModel):
    target: str = Field(min_length=1)
    relation_type: str = Field(
        description="related_to | depends_on | contradicts | supersedes | mentions"
    )
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: str = Field(min_length=1)


# ── Router ─────────────────────────────────────────────────────────────────


def create_memory_router(get_service: Any) -> APIRouter:
    """Create the memory router with dependency injection."""
    router = APIRouter(prefix="/api/memory", tags=["memory"])

    def _store(service):
        return getattr(service, "_memory_store", None)

    def _recall(service):
        return getattr(service, "_memory_recall_service", None)

    def _invalidate(service, session_id: str | None = None) -> None:
        ctx = getattr(service, "_memory_context", None)
        if ctx is not None and hasattr(ctx, "invalidate_cache"):
            ctx.invalidate_cache(session_id)

    # ── GET /api/memory ─────────────────────────────────────────────────

    @router.get("", response_model=MemoryListResponse)
    async def list_memories(
        type: str | None = None,
        status: str | None = None,
        scope: str | None = None,
        confidence_min: float | None = None,
        q: str = "",
        _expand: bool = False,
        limit: int = 100,
        offset: int = 0,
        service=Depends(get_service),
    ) -> dict:
        """List all memories with optional filters and aggregate overview.

        **Query Parameters:**
        - ``type``/``status``/``scope``: Filter by metadata.
        - ``confidence_min``: Minimum confidence (0-1).
        - ``q`` (str): Full-text search in name, description, and content.
        - ``_expand`` (bool): Include ``content`` and ``created_at`` fields.
        - ``limit``/``offset``: Pagination.
        """
        store = _store(service)
        if store is None:
            return {"items": [], "overview": {}}

        summaries = store.list_memories()
        items = []
        for s in summaries:
            mem = store.read_memory(s.name)
            if mem is None:
                continue
            meta = mem.metadata
            if type and meta.type != type: continue
            if status and meta.status != status: continue
            if scope and meta.scope != scope: continue
            if confidence_min is not None and meta.confidence < confidence_min: continue
            if q:
                ql = q.lower()
                if ql not in mem.name.lower() and ql not in mem.description.lower() and ql not in mem.content.lower():
                    continue
            item: dict = {
                "name": mem.name, "description": mem.description,
                "type": meta.type, "status": meta.status,
                "scope": meta.scope, "confidence": meta.confidence,
                "importance": meta.importance,
                "access_count": meta.access_count,
                "updated_at": mem.updated_at,
            }
            if _expand:
                item["content"] = mem.content
                item["created_at"] = mem.created_at
            items.append(item)
        overview = store.get_stats()
        overview.setdefault("enabled", True)
        overview.setdefault("preview", False)
        return {"items": items[:limit], "overview": overview}

    # ── GET /api/memory/search ─────────────────────────────────────────
    # NOTE: MUST be placed BEFORE /{name} or FastAPI matches "search" as name.

    @router.get("/search")
    async def search_memories(
        q: str = "",
        top_k: int = 5,
        service=Depends(get_service),
    ) -> list[dict]:
        """Semantic search across memories.

        **Query Parameters:**
        - ``q`` (str): Natural language query.
        - ``top_k`` (int, default 5): Max results.

        **Response (200):** Array of ``{name, content, score}``.
        """
        if not q.strip():
            return []
        ext = getattr(service, "_external_store", None)
        if ext is None:
            return []
        try:
            results = ext.search(q, top_k=top_k, min_score=0.0)
            return [
                {"name": r.get("name", ""), "content": r.get("content", "")[:500],
                 "score": round(r.get("score", 0), 3)}
                for r in results
            ]
        except Exception as exc:
            logger.warning("Semantic search failed: %s", exc)
            return []

    # ── GET /api/memory/stats ──────────────────────────────────────────
    # NOTE: MUST be placed BEFORE /{name} or FastAPI matches "stats" as name.

    @router.get("/stats")
    async def memory_stats(
        service=Depends(get_service),
    ) -> dict:
        """Get aggregate memory statistics via SQL COUNT."""
        store = _store(service)
        if store is None:
            return {"total": 0, "active": 0, "deprecated": 0, "by_type": {}, "by_scope": {}, "by_layer": {}}
        return store.get_stats()

    # ── Session recall APIs ──────────────────────────────────────────────

    @router.get("/sessions/{session_id}/recalls")
    async def list_session_recalls(
        session_id: str,
        limit: int = 50,
        service=Depends(get_service),
    ) -> dict:
        recall = _recall(service)
        if recall is None:
            return {"session_id": session_id, "items": []}
        return {"session_id": session_id, "items": recall.list_recalls(session_id, limit=limit)}

    @router.post("/sessions/{session_id}/preview-recall")
    async def preview_session_recall(
        session_id: str,
        body: RecallPreviewRequest,
        service=Depends(get_service),
    ) -> dict:
        recall = _recall(service)
        if recall is None:
            return {"session_id": session_id, "items": [], "injection_text": ""}
        from memory.recall import MemoryRecallQuery
        result = recall.recall(
            MemoryRecallQuery(
                session_id=session_id,
                user_message=body.query,
                task_description=body.query,
                top_k=body.top_k,
            ),
            record=False,
        )
        return result.to_dict()

    @router.get("/sessions/{session_id}/generated")
    async def list_generated_memories(
        session_id: str,
        limit: int = 50,
        service=Depends(get_service),
    ) -> dict:
        recall = _recall(service)
        if recall is None:
            return {"session_id": session_id, "items": []}
        return {"session_id": session_id, "items": recall.list_generated(session_id, limit=limit)}

    @router.post("/sessions/{session_id}/overrides")
    async def set_memory_override(
        session_id: str,
        body: MemoryOverrideRequest,
        service=Depends(get_service),
    ) -> dict:
        recall = _recall(service)
        if recall is None:
            raise HTTPException(status_code=503, detail="Memory recall service not available")
        result = recall.set_override(session_id, body.memory_name, body.action)
        _invalidate(service, session_id)
        return result

    # ── GET /api/memory/{name} ──────────────────────────────────────────

    @router.get("/{name}", response_model=MemoryDetailResponse)
    async def get_memory(
        name: str,
        service=Depends(get_service),
    ) -> dict:
        """Get a single memory with full content."""
        store = _store(service)
        if store is None:
            raise HTTPException(status_code=503, detail="Memory store not available")
        mem = store.read_memory(name)
        if mem is None:
            raise HTTPException(status_code=404, detail=f"Memory not found: {name}")
        return {
            "name": mem.name, "description": mem.description,
            "content": mem.content, "type": mem.metadata.type,
            "status": mem.metadata.status, "scope": mem.metadata.scope,
            "confidence": mem.metadata.confidence,
            "importance": mem.metadata.importance,
            "current_revision": (
                store.list_revisions(name)[0]["revision"]
                if store.list_revisions(name) else 1
            ),
            "access_count": mem.metadata.access_count,
            "source": getattr(mem, "source", ""),
            "source_session_id": getattr(mem, "source_session_id", ""),
            "created_at": mem.created_at,
            "updated_at": mem.updated_at,
            "anchors": [a.to_dict() for a in mem.anchors],
        }

    # ── POST /api/memory ───────────────────────────────────────────────

    @router.post("", status_code=201)
    async def create_memory(
        body: MemoryCreateRequest,
        service=Depends(get_service),
    ) -> dict:
        """Create a new memory (file + DB)."""
        store = _store(service)
        if store is None:
            raise HTTPException(status_code=503, detail="Memory store not available")

        from memory.models import Memory, MemoryMetadata, MemoryType, MemoryStatus, MemoryScope, Anchor

        anchors = [Anchor(**a) for a in body.anchors] if body.anchors else []
        mem = Memory(
            name=body.name,
            description=body.description,
            content=body.content,
            metadata=MemoryMetadata(
                type=MemoryType(body.type) if body.type in ("user", "feedback", "project", "reference") else MemoryType.PROJECT,
                status=MemoryStatus.ACTIVE,
                scope=MemoryScope.PROJECT,
                confidence=body.confidence,
                importance=body.importance,
            ),
            anchors=anchors,
        )
        ok = store.write_memory(mem, source="web_api", source_session_id=body.source_session_id or "")
        if not ok:
            raise HTTPException(status_code=409, detail=f"Memory '{body.name}' already exists")
        _invalidate(service)
        return {"name": body.name, "status": "created", **store.last_write_result}

    # ── PATCH /api/memory/{name} ───────────────────────────────────────

    @router.patch("/{name}")
    async def update_memory(
        name: str,
        body: MemoryUpdateRequest,
        service=Depends(get_service),
    ) -> dict:
        """Update an existing memory (file + DB)."""
        store = _store(service)
        if store is None:
            raise HTTPException(status_code=503, detail="Memory store not available")
        mem = store.read_memory(name)
        if mem is None:
            raise HTTPException(status_code=404, detail=f"Memory not found: {name}")

        changed = False
        if body.description is not None:
            mem.description = body.description; changed = True
        if body.content is not None:
            mem.content = body.content; changed = True
        if body.confidence is not None:
            mem.metadata.confidence = body.confidence; changed = True
        if body.importance is not None:
            mem.metadata.importance = body.importance; changed = True
        if body.type is not None:
            from memory.models import MemoryType
            mem.metadata.type = MemoryType(body.type) if body.type in ("user", "feedback", "project", "reference") else mem.metadata.type
            changed = True
        if body.status is not None:
            from memory.models import MemoryStatus
            mem.metadata.status = MemoryStatus(body.status) if body.status in ("active", "deprecated") else MemoryStatus.ACTIVE
            changed = True
        if body.anchors is not None:
            from memory.models import Anchor
            mem.anchors = [Anchor(**a) for a in body.anchors]
            changed = True
        if body.source_session_id is not None:
            changed = True  # stored via write_memory source_session_id (not in model yet)
        if changed:
            store.write_memory(mem, source="web_api")
            _invalidate(service)
        return {
            "name": name, "status": "updated", "changed": changed,
            **(store.last_write_result if changed else {"action": "NOOP"}),
        }

    @router.get("/{name}/revisions")
    async def list_memory_revisions(
        name: str,
        service=Depends(get_service),
    ) -> dict:
        store = _store(service)
        if store is None:
            raise HTTPException(status_code=503, detail="Memory store not available")
        if store.read_memory(name) is None:
            raise HTTPException(status_code=404, detail=f"Memory not found: {name}")
        return {"name": name, "items": store.list_revisions(name)}

    @router.get("/{name}/edges")
    async def list_memory_edges(
        name: str,
        service=Depends(get_service),
    ) -> dict:
        store = _store(service)
        if store is None:
            raise HTTPException(status_code=503, detail="Memory store not available")
        return {"name": name, "items": store.list_edges(name)}

    @router.post("/{name}/edges", status_code=201)
    async def create_memory_edge(
        name: str,
        body: MemoryEdgeRequest,
        service=Depends(get_service),
    ) -> dict:
        store = _store(service)
        if store is None:
            raise HTTPException(status_code=503, detail="Memory store not available")
        try:
            return store.upsert_edge(
                name, body.target, body.relation_type, body.confidence, body.evidence
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ── DELETE /api/memory/{name} ──────────────────────────────────────

    @router.delete("/{name}")
    async def delete_memory(
        name: str,
        service=Depends(get_service),
    ) -> dict:
        """Delete a memory (file + DB)."""
        store = _store(service)
        if store is None:
            raise HTTPException(status_code=503, detail="Memory store not available")
        if store.read_memory(name) is None:
            raise HTTPException(status_code=404, detail=f"Memory not found: {name}")
        store.delete_memory(name)
        _invalidate(service)
        return {"name": name, "deleted": True}

    # ── GET /api/memory/{name}/edges ────────────────────────────────────

    @router.get("/{name}/edges")
    async def get_memory_edges(
        name: str,
        service=Depends(get_service),
    ) -> list[dict]:
        """Get entity links (edges) for a memory.

        Returns both outgoing (source) and incoming (target) relationships.
        Each edge has: source_name, target_name, relation_type, confidence, evidence.
        """
        store = _store(service)
        if store is None:
            return []
        mem = store.read_memory(name)
        if mem is None:
            raise HTTPException(status_code=404, detail=f"Memory not found: {name}")
        backend = getattr(store, '_backend', None)
        if backend is None or not hasattr(backend, 'list_edges'):
            return []
        return backend.list_edges(name)

    # ── GET /api/memory/{name}/revisions ────────────────────────────────

    @router.get("/{name}/revisions")
    async def get_memory_revisions(
        name: str,
        service=Depends(get_service),
    ) -> list[dict]:
        """Get revision history for a memory.

        Returns all revisions ordered by revision DESC (newest first).
        Each revision has: revision, content_hash, payload_json, source, created_at.
        """
        store = _store(service)
        if store is None:
            return []
        mem = store.read_memory(name)
        if mem is None:
            raise HTTPException(status_code=404, detail=f"Memory not found: {name}")
        backend = getattr(store, '_backend', None)
        if backend is None or not hasattr(backend, 'list_revisions'):
            return []
        return backend.list_revisions(name)

    return router
