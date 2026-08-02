"""Read-only safety and authority API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query


def create_safety_router(get_service: Any) -> APIRouter:
    router = APIRouter(prefix="/api/safety", tags=["safety"])

    @router.get("")
    async def get_safety_snapshot(
        session_id: str = Query(default="", max_length=128),
        service=Depends(get_service),
    ) -> dict:
        try:
            return service._safety_service.get_snapshot(session_id.strip())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
