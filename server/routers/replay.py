"""Replay evidence and isolated tool re-execution API."""

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

    @router.post("/{session_id}/runs/{run_id}/executions", status_code=202)
    async def start_replay_execution(
        session_id: str,
        run_id: str,
        service=Depends(get_service),
    ) -> dict:
        try:
            return service._replay_service.start_execution(session_id, run_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/executions/{execution_id}")
    async def get_replay_execution(
        execution_id: str,
        service=Depends(get_service),
    ) -> dict:
        try:
            return service._replay_service.get_execution(execution_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/executions/{execution_id}/pin")
    async def pin_replay_execution(
        execution_id: str,
        service=Depends(get_service),
    ) -> dict:
        try:
            return service._replay_service.pin_execution(execution_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/executions/{execution_id}/workspace")
    async def delete_replay_workspace(
        execution_id: str,
        service=Depends(get_service),
    ) -> dict:
        try:
            return service._replay_service.delete_workspace(execution_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
