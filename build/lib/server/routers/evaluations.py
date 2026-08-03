"""Read-only evaluation and regression artifact API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends


def create_evaluations_router(get_service: Any) -> APIRouter:
    router = APIRouter(prefix="/api/evaluations", tags=["evaluations"])

    @router.get("")
    async def get_evaluation_overview(
        service=Depends(get_service),
    ) -> dict:
        return service._evaluation_service.get_overview()

    return router
