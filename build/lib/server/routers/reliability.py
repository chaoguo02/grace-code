"""Read-only project reliability API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query


def create_reliability_router(get_service: Any) -> APIRouter:
    router = APIRouter(prefix="/api/reliability", tags=["reliability"])

    @router.get("")
    async def get_reliability_overview(
        days: int = Query(default=30, ge=1, le=90),
        service=Depends(get_service),
    ) -> dict:
        return service._reliability_service.get_overview(days)

    return router
