"""Multi-agent review API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from server.schemas.review import ReviewJobResponse, StartReviewRequest


def create_reviews_router(get_service: Any) -> APIRouter:
    router = APIRouter(prefix="/api/reviews", tags=["reviews"])

    @router.post(
        "/sessions/{session_id}",
        response_model=ReviewJobResponse,
        status_code=202,
    )
    async def start_review(
        session_id: str,
        body: StartReviewRequest,
        service=Depends(get_service),
    ):
        try:
            return service._review_service.start_review(
                session_id,
                focus=body.focus,
                max_agents=body.max_agents,
            )
        except ValueError as exc:
            detail = str(exc)
            status = 404 if detail.startswith("Unknown session") else 409
            raise HTTPException(status_code=status, detail=detail)

    @router.get(
        "/sessions/{session_id}/latest",
        response_model=ReviewJobResponse | None,
    )
    async def latest_review(
        session_id: str,
        service=Depends(get_service),
    ):
        if service.session_service.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return service._review_service.get_latest_review(session_id)

    @router.get("/{job_id}", response_model=ReviewJobResponse)
    async def get_review(job_id: str, service=Depends(get_service)):
        try:
            return service._review_service.get_review(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @router.post(
        "/{job_id}/cancel",
        response_model=ReviewJobResponse,
        status_code=202,
    )
    async def cancel_review(job_id: str, service=Depends(get_service)):
        try:
            return service._review_service.cancel_review(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @router.post(
        "/{job_id}/retry",
        response_model=ReviewJobResponse,
        status_code=202,
    )
    async def retry_review(job_id: str, service=Depends(get_service)):
        try:
            return service._review_service.retry_review(job_id)
        except ValueError as exc:
            detail = str(exc)
            status = 404 if detail.startswith("Unknown review") else 409
            raise HTTPException(status_code=status, detail=detail)

    @router.post(
        "/{job_id}/tasks/{task_id}/retry",
        response_model=ReviewJobResponse,
        status_code=202,
    )
    async def retry_review_task(
        job_id: str,
        task_id: str,
        service=Depends(get_service),
    ):
        try:
            return service._review_service.retry_task(job_id, task_id)
        except ValueError as exc:
            detail = str(exc)
            status = (
                404
                if detail.startswith(("Unknown review", "Unknown review task"))
                else 409
            )
            raise HTTPException(status_code=status, detail=detail)

    @router.delete(
        "/{job_id}/snapshot",
        response_model=ReviewJobResponse,
    )
    async def release_review_snapshot(
        job_id: str,
        service=Depends(get_service),
    ):
        try:
            return service._review_service.release_snapshot(job_id)
        except ValueError as exc:
            detail = str(exc)
            status = 404 if detail.startswith("Unknown review") else 409
            raise HTTPException(status_code=status, detail=detail)

    return router
