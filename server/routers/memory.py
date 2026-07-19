"""
Memory router — CRUD for long-term memory, backed by SQLite.

Mounted under ``/api/memory``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ── Schemas ────────────────────────────────────────────────────────────────


class MemoryItemResponse(BaseModel):
    """A single memory item (summary, no content)."""

    name: str = Field(description="Memory slug.")
    description: str = Field(description="One-line summary.")
    type: str = Field(description="user | feedback | project | reference.")
    status: str = Field(description="active | deprecated.")
    scope: str = Field(description="session | project | global.")
    confidence: float = Field(description="0.0–1.0.")
    access_count: int = Field(description="Times accessed.")
    updated_at: str = Field(description="ISO-8601.")


class MemoryDetailResponse(MemoryItemResponse):
    """Full memory with content."""

    content: str = Field(description="Markdown body.")
    source: str = Field(default="", description="Origin.")
    source_session_id: str = Field(default="", description="Session that created it.")


class MemoryListResponse(BaseModel):
    """List + overview."""

    items: list[MemoryItemResponse]
    overview: dict = Field(description="Aggregate stats.")


class MemoryCreateRequest(BaseModel):
    """Request body for POST /api/memory."""

    name: str = Field(min_length=1, description="Slug.")
    description: str = Field(min_length=1, description="One-line summary.")
    content: str = Field(default="", description="Markdown body.")
    type: str = Field(default="project", description="user | feedback | project | reference.")
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    anchors: list[dict] = Field(default_factory=list, description="File/symbol anchors.")


class MemoryUpdateRequest(BaseModel):
    """Request body for PATCH /api/memory/{name}."""

    description: str | None = None
    content: str | None = None
    type: str | None = None
    status: str | None = None
    confidence: float | None = None


# ── Router ─────────────────────────────────────────────────────────────────


def create_memory_router(get_service: Any) -> APIRouter:
    """Create the memory router with dependency injection."""
    router = APIRouter(prefix="/api/memory", tags=["memory"])

    def _store(service):
        return getattr(service, "_memory_store", None)

    # ── GET /api/memory ─────────────────────────────────────────────────

    @router.get("", response_model=MemoryListResponse)
    async def list_memories(
        type: str | None = None,
        status: str | None = None,
        scope: str | None = None,
        confidence_min: float | None = None,
        limit: int = 100,
        offset: int = 0,
        service=Depends(get_service),
    ) -> dict:
        """List all memories with optional filters and aggregate overview."""
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
            items.append({
                "name": mem.name, "description": mem.description,
                "type": meta.type, "status": meta.status,
                "scope": meta.scope, "confidence": meta.confidence,
                "access_count": meta.access_count,
                "updated_at": mem.updated_at,
            })
        by_type = store.count_by_type()
        overview = {
            "total": len(summaries), "active": sum(1 for i in items if i["status"] == "active"),
            "deprecated": sum(1 for i in items if i["status"] == "deprecated"),
            "expiring": 0, "enabled": True, "preview": False,
            "by_type": by_type,
            "by_scope": {}, "by_layer": {},
        }
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
            "access_count": mem.metadata.access_count,
            "source": "", "source_session_id": "",
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
            ),
            anchors=anchors,
        )
        ok = store.write_memory(mem, source="web_api")
        if not ok:
            raise HTTPException(status_code=409, detail=f"Memory '{body.name}' already exists")
        return {"name": body.name, "status": "created"}

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
        if body.type is not None:
            from memory.models import MemoryType
            mem.metadata.type = MemoryType(body.type) if body.type in ("user", "feedback", "project", "reference") else mem.metadata.type
            changed = True
        if body.status is not None:
            from memory.models import MemoryStatus
            mem.metadata.status = MemoryStatus(body.status) if body.status in ("active", "deprecated") else MemoryStatus.ACTIVE
            changed = True
        if changed:
            store.write_memory(mem, source="web_api")
        return {"name": name, "status": "updated", "changed": changed}

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
        ok = store.delete_memory(name)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Memory not found: {name}")
        return {"name": name, "deleted": True}

    return router
