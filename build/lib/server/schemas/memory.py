"""
Pydantic schemas for memory API endpoints.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MemoryItemResponse(BaseModel):
    """A single memory item (summary, no content)."""

    name: str = Field(description="Memory slug.")
    description: str = Field(description="One-line summary.")
    type: str = Field(description="user | feedback | project | reference.")
    status: str = Field(description="active | deprecated.")
    scope: str = Field(description="session | project | global.")
    confidence: float = Field(description="0.0-1.0.")
    importance: float = Field(default=0.5, description="0.0-1.0.")
    access_count: int = Field(description="Times accessed.")
    created_at: str = Field(default="", description="ISO-8601.")
    updated_at: str = Field(description="ISO-8601.")
    content: str | None = Field(default=None, description="Markdown body (only with _expand).")


class MemoryDetailResponse(BaseModel):
    """Full memory with content."""

    name: str = Field(description="Memory slug.")
    description: str = Field(description="One-line summary.")
    type: str = Field(description="user | feedback | project | reference.")
    status: str = Field(description="active | deprecated.")
    scope: str = Field(description="session | project | global.")
    confidence: float = Field(description="0.0-1.0.")
    importance: float = Field(default=0.5, description="0.0-1.0.")
    current_revision: int = Field(default=1, ge=1)
    access_count: int = Field(description="Times accessed.")
    content: str = Field(description="Markdown body.")
    source: str = Field(default="", description="Origin.")
    source_session_id: str = Field(default="", description="Session that created it.")
    created_at: str = Field(default="", description="ISO-8601.")
    updated_at: str = Field(description="ISO-8601.")
    anchors: list[dict] = Field(default_factory=list, description="File/symbol anchors.")


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
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    source_session_id: str = Field(default="", description="Session that created this memory.")
    anchors: list[dict] = Field(default_factory=list, description="File/symbol anchors.")


class MemoryUpdateRequest(BaseModel):
    """Request body for PATCH /api/memory/{name}."""

    description: str | None = None
    content: str | None = None
    type: str | None = None
    status: str | None = None
    confidence: float | None = None
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    source_session_id: str | None = None
    anchors: list[dict] | None = None
