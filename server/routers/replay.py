"""Read-only replay and failure-boundary API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException


def create_replay_router(get_service: Any) -> APIRouter:
    router = APIRouter(prefix="/api/replay", tags=["replay"])

    @router.get("/{session_id}")
    async def get_session_replay(
        session_id: str,
        service=Depends(get_service),
    ) -> dict:
        try:
            return service._replay_service.get_session_replay(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
