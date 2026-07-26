"""Read-only runtime architecture API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query


def create_architecture_router(get_service: Any) -> APIRouter:
    router = APIRouter(prefix="/api/architecture", tags=["architecture"])

    @router.get("")
    async def get_architecture(
        session_id: str = Query(default="", max_length=128),
        service=Depends(get_service),
    ) -> dict:
        try:
            return service._architecture_service.get_snapshot(
                session_id=session_id.strip(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
