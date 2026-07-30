"""Multi-agent control-plane inspection and explicit user actions."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException


def _http_status_for(exc: Exception, not_found_hint: bool = False) -> int:
    """Map domain exceptions to HTTP status codes.

    - PermissionError → 403 Forbidden
    - TypeError → 422 Unprocessable Entity (bad input shape)
    - ValueError with a "not found" / "unknown" message → 404
    - ValueError (other) → 422 Unprocessable Entity
    - RuntimeError → 409 Conflict (runtime state)
    """
    if isinstance(exc, PermissionError):
        return 403
    if isinstance(exc, TypeError):
        return 422
    if isinstance(exc, ValueError):
        detail = str(exc).lower()
        if not_found_hint or "not found" in detail or "unknown" in detail:
            return 404
        return 422
    if isinstance(exc, RuntimeError):
        return 409
    return 500


def create_multi_agent_router(get_service: Any) -> APIRouter:
    router = APIRouter(prefix="/api/multi-agent", tags=["multi-agent"])

    @router.get("/catalog/definitions")
    async def list_agent_definitions(
        service=Depends(get_service),
    ) -> dict:
        definitions = []
        for spec in service._runtime.agent_registry.list_all():
            definitions.append({
                "name": spec.name,
                "description": spec.description,
                "kind": spec.agent_kind.value,
                "intent": spec.intent.value,
                "visibility": spec.visibility.value,
                "workspace_mode": spec.workspace_mode.value,
                "model": spec.model,
                "tools": sorted(spec.tools),
                "disallowed_tools": sorted(spec.disallowed_tools),
                "allowed_subagents": sorted(
                    spec.delegation_policy.allowed_names
                ),
                "max_turns": spec.max_turns,
                "max_tokens": spec.max_tokens,
                "background": spec.background,
                "skills": list(spec.skills),
                "mcp_servers": list(spec.mcp_servers),
            })
        return {"definitions": definitions}

    @router.get("/{session_id}")
    async def get_multi_agent_snapshot(
        session_id: str,
        service=Depends(get_service),
    ) -> dict:
        try:
            return service._multi_agent_service.get_snapshot(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/{session_id}/runs/{run_id}")
    async def get_delegation_run(
        session_id: str,
        run_id: str,
        service=Depends(get_service),
    ) -> dict:
        try:
            return service._multi_agent_service.get_run(session_id, run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/{session_id}/runs/{run_id}/resume")
    async def resume_delegation_run(
        session_id: str,
        run_id: str,
        service=Depends(get_service),
    ) -> dict:
        try:
            return await asyncio.to_thread(
                service._multi_agent_service.resume_run,
                session_id,
                run_id,
            )
        except (TypeError, ValueError, RuntimeError, PermissionError) as exc:
            raise HTTPException(
                status_code=_http_status_for(exc), detail=str(exc),
            ) from exc

    @router.post("/{session_id}/runs/{run_id}/integrate")
    async def integrate_delegation_run(
        session_id: str,
        run_id: str,
        body: dict = Body(...),
        service=Depends(get_service),
    ) -> dict:
        try:
            return await asyncio.to_thread(
                service._multi_agent_service.integrate_run,
                session_id,
                run_id,
                list(body.get("decisions", [])),
            )
        except (TypeError, ValueError, RuntimeError, PermissionError) as exc:
            raise HTTPException(
                status_code=_http_status_for(exc), detail=str(exc),
            ) from exc

    @router.post("/{session_id}/runs/{run_id}/verify")
    async def verify_delegation_run(
        session_id: str,
        run_id: str,
        service=Depends(get_service),
    ) -> dict:
        try:
            return await asyncio.to_thread(
                service._multi_agent_service.verify_run,
                session_id,
                run_id,
            )
        except (TypeError, ValueError, RuntimeError, PermissionError) as exc:
            raise HTTPException(
                status_code=_http_status_for(exc), detail=str(exc),
            ) from exc

    @router.post("/{session_id}/team/propose")
    async def propose_team(
        session_id: str,
        body: dict = Body(...),
        service=Depends(get_service),
    ) -> dict:
        try:
            return service._runtime.propose_agent_team(
                session_id=session_id,
                members=list(body.get("members", [])),
                tasks=list(body.get("tasks", [])),
            )
        except RuntimeError as exc:
            raise HTTPException(
                status_code=_http_status_for(exc), detail=str(exc),
            ) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/{session_id}/team/approve")
    async def approve_team(
        session_id: str,
        service=Depends(get_service),
    ) -> dict:
        try:
            return service._runtime.approve_agent_team(session_id=session_id)
        except (TypeError, ValueError, RuntimeError, PermissionError) as exc:
            raise HTTPException(
                status_code=_http_status_for(exc), detail=str(exc),
            ) from exc

    @router.post("/{session_id}/team/reject")
    async def reject_team(
        session_id: str,
        service=Depends(get_service),
    ) -> dict:
        try:
            return service._runtime.reject_agent_team(session_id=session_id)
        except (TypeError, ValueError, RuntimeError, PermissionError) as exc:
            raise HTTPException(
                status_code=_http_status_for(exc), detail=str(exc),
            ) from exc

    @router.post("/{session_id}/team/message")
    async def team_message(
        session_id: str,
        body: dict = Body(...),
        service=Depends(get_service),
    ) -> dict:
        try:
            return service._runtime.send_team_message(
                session_id=session_id,
                sender_id=str(body.get("sender_id", "")),
                recipient_id=str(body.get("recipient_id", "")),
                body=str(body.get("body", "")),
            )
        except (TypeError, ValueError, RuntimeError, PermissionError) as exc:
            raise HTTPException(
                status_code=_http_status_for(exc), detail=str(exc),
            ) from exc

    @router.post("/{session_id}/team/shutdown")
    async def shutdown_team(
        session_id: str,
        body: dict = Body(default={}),
        service=Depends(get_service),
    ) -> dict:
        try:
            return service._runtime.shutdown_agent_team(
                session_id=session_id,
                cancel=bool(body.get("cancel", False)),
            )
        except (TypeError, ValueError, RuntimeError, PermissionError) as exc:
            raise HTTPException(
                status_code=_http_status_for(exc), detail=str(exc),
            ) from exc

    @router.post("/{session_id}/team/tasks/{task_id}/claim")
    async def claim_team_task(
        session_id: str,
        task_id: str,
        body: dict = Body(...),
        service=Depends(get_service),
    ) -> dict:
        try:
            return service._runtime.claim_team_task(
                session_id=session_id,
                task_id=task_id,
                member_id=str(body.get("member_id", "")),
            )
        except (TypeError, ValueError, RuntimeError, PermissionError) as exc:
            raise HTTPException(
                status_code=_http_status_for(exc), detail=str(exc),
            ) from exc

    @router.post("/{session_id}/team/tasks/{task_id}/complete")
    async def complete_team_task(
        session_id: str,
        task_id: str,
        body: dict = Body(...),
        service=Depends(get_service),
    ) -> dict:
        try:
            return service._runtime.complete_team_task(
                session_id=session_id,
                task_id=task_id,
                member_id=str(body.get("member_id", "")),
                lease_token=str(body.get("lease_token", "")),
                summary=str(body.get("summary", "")),
                failed=bool(body.get("failed", False)),
            )
        except (TypeError, ValueError, RuntimeError, PermissionError) as exc:
            raise HTTPException(
                status_code=_http_status_for(exc), detail=str(exc),
            ) from exc

    @router.post("/{session_id}/team/tasks/{task_id}/execute")
    async def execute_team_task(
        session_id: str,
        task_id: str,
        body: dict = Body(...),
        service=Depends(get_service),
    ) -> dict:
        try:
            result = await asyncio.to_thread(
                service._runtime.execute_team_task,
                session_id=session_id,
                task_id=task_id,
                member_id=str(body.get("member_id", "")),
                lease_token=str(body.get("lease_token", "")),
            )
            return {
                "task_id": task_id,
                "child_session_id": result.session_id,
                "status": result.status.value,
                "summary": result.summary,
                "tokens_used": result.tokens_used,
            }
        except (TypeError, ValueError, RuntimeError, PermissionError) as exc:
            raise HTTPException(
                status_code=_http_status_for(exc), detail=str(exc),
            ) from exc

    @router.post("/{session_id}/team/tasks/{task_id}/resolve")
    async def resolve_team_task(
        session_id: str,
        task_id: str,
        body: dict = Body(...),
        service=Depends(get_service),
    ) -> dict:
        try:
            return service._runtime.resolve_team_task_review(
                session_id=session_id,
                task_id=task_id,
                accepted=bool(body.get("accepted", False)),
                summary=str(body.get("summary", "")),
            )
        except (TypeError, ValueError, RuntimeError, PermissionError) as exc:
            raise HTTPException(
                status_code=_http_status_for(exc), detail=str(exc),
            ) from exc

    @router.post("/{session_id}/delegations/{run_id}/cancel")
    @router.post("/{session_id}/runs/{run_id}/cancel")
    async def cancel_delegation_run(
        session_id: str,
        run_id: str,
        body: dict = Body(default={}),
        service=Depends(get_service),
    ) -> dict:
        try:
            return service._multi_agent_service.cancel_run(
                session_id,
                run_id,
                str(body.get("detail", "User cancelled delegation run")),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=_http_status_for(exc, not_found_hint=True),
                detail=str(exc),
            ) from exc

    @router.post("/{session_id}/tasks/{task_id}/cancel")
    async def cancel_delegation_task(
        session_id: str,
        task_id: str,
        body: dict = Body(default={}),
        service=Depends(get_service),
    ) -> dict:
        task = service._store.get_delegation_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Unknown delegation task")
        run = service._store.get_delegation_run(
            str(task["delegation_run_id"])
        )
        if run is None or str(run["parent_session_id"]) != session_id:
            raise HTTPException(status_code=404, detail="Task is outside session")
        child_id = str(task.get("child_session_id") or "")
        if not child_id:
            changed = service._store.update_delegation_task(
                task_id,
                status="cancelled",
                error="Cancelled before start",
                expected_statuses=("queued",),
            )
            if not changed:
                raise HTTPException(
                    status_code=409,
                    detail="Delegation task already started or converged",
                )
            service._store.reconcile_delegation_run(
                str(task["delegation_run_id"])
            )
            return {"task_id": task_id, "status": "cancelled"}
        try:
            result = service._runtime.cancel_agent(
                parent_session_id=session_id,
                child_session_id=child_id,
                detail=str(body.get("detail", "User cancelled delegation task")),
            )
            return {
                "task_id": task_id,
                "child_session_id": child_id,
                "outcome": result.outcome.value,
                "status": result.session_status.value,
            }
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=_http_status_for(exc), detail=str(exc),
            ) from exc

    @router.post("/{session_id}/tasks/{task_id}/retry")
    async def retry_delegation_task(
        session_id: str,
        task_id: str,
        service=Depends(get_service),
    ) -> dict:
        try:
            return await asyncio.to_thread(
                service._multi_agent_service.retry_task,
                session_id,
                task_id,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=_http_status_for(exc, not_found_hint=True),
                detail=str(exc),
            ) from exc
        except (TypeError, RuntimeError, PermissionError) as exc:
            raise HTTPException(
                status_code=_http_status_for(exc), detail=str(exc),
            ) from exc

    return router
