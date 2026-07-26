"""Read-only project overview API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query


def create_overview_router(get_service: Any) -> APIRouter:
    router = APIRouter(prefix="/api/overview", tags=["overview"])

    @router.get("")
    async def get_project_overview(
        session_id: str = Query(default="", max_length=128),
        service=Depends(get_service),
    ) -> dict:
        return service._project_overview_service.get_overview(
            session_id.strip()
        )

    return router
