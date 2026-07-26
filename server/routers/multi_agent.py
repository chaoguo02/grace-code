"""Multi-agent control-plane inspection and explicit user actions."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException


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
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/{session_id}/team/reject")
    async def reject_team(
        session_id: str,
        service=Depends(get_service),
    ) -> dict:
        try:
            return service._runtime.reject_agent_team(session_id=session_id)
        except (TypeError, ValueError, RuntimeError, PermissionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
            service._store.update_delegation_task(
                task_id, status="cancelled", error="Cancelled before start",
            )
            siblings = service._store.list_delegation_tasks(
                str(task["delegation_run_id"])
            )
            terminal = {
                "completed", "partial", "failed", "cancelled",
                "no_findings", "budget_exhausted", "rejected", "superseded",
            }
            if siblings and all(
                str(item["status"]) in terminal for item in siblings
            ):
                service._store.complete_delegation_run(
                    str(task["delegation_run_id"]),
                    status=(
                        "partial"
                        if any(
                            bool(item["required"])
                            and str(item["status"]) not in {
                                "completed", "no_findings",
                            }
                            for item in siblings
                        )
                        else "completed"
                    ),
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
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/{session_id}/tasks/{task_id}/retry")
    async def retry_delegation_task(
        session_id: str,
        task_id: str,
        service=Depends(get_service),
    ) -> dict:
        from agent.session.models import ExplicitDelegationRequest
        from agent.session.task_contract import TaskContract

        task = service._store.get_delegation_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Unknown delegation task")
        original_run = service._store.get_delegation_run(
            str(task["delegation_run_id"])
        )
        if (
            original_run is None
            or str(original_run["parent_session_id"]) != session_id
        ):
            raise HTTPException(status_code=404, detail="Task is outside session")
        if str(task["status"]) not in {
            "failed", "cancelled", "partial", "budget_exhausted",
        }:
            raise HTTPException(
                status_code=409,
                detail="Only a terminal incomplete task can be retried",
            )
        parent = service._store.get_session(session_id)
        if parent is None:
            raise HTTPException(status_code=404, detail="Unknown session")
        definition = service._runtime.agent_registry.get(
            str(task["agent_type"])
        )
        retry_run_id = f"delegation-retry-{uuid.uuid4().hex}"
        retry_task_id = f"{retry_run_id}:retry"
        service._store.create_delegation_run(
            run_id=retry_run_id,
            parent_session_id=session_id,
            topology="one_to_one",
            reason_code="explicit_retry",
            explanation=f"Retry of {task_id}",
            budget={"source_task_id": task_id},
        )
        service._store.create_delegation_task(
            task_id=retry_task_id,
            delegation_run_id=retry_run_id,
            agent_type=str(task["agent_type"]),
            purpose=str(task["purpose"]),
            goal=str(task["goal"]),
            prompt=str(task.get("prompt") or task["goal"]),
            scope=tuple(str(item) for item in task["scope"]),
            expected_files=tuple(
                str(item) for item in task["expected_files"]
            ),
            write_files=tuple(str(item) for item in task["write_files"]),
            required=bool(task["required"]),
        )

        def created(child) -> None:
            service._store.update_delegation_task(
                retry_task_id,
                status="running",
                child_session_id=child.id,
                generation=int(child.generation),
            )

        contract = TaskContract.for_subagent(
            definition,
            service._runtime._root_agent_config,
            parent_budget_tokens=min(
                service._runtime._root_agent_config.budget_tokens,
                definition.max_tokens
                or service._runtime._root_agent_config.budget_tokens,
            ),
            parent_max_steps=service._runtime._root_agent_config.max_steps,
        )
        try:
            result = await asyncio.to_thread(
                service._runtime.run_explicit_delegation,
                session_id,
                request=ExplicitDelegationRequest(
                    agent_name=definition.name,
                    description=str(task["goal"])[:80],
                    prompt=str(task.get("prompt") or task["goal"]),
                ),
                parent_intent=service._runtime.agent_registry.get(
                    parent.agent_name
                ).intent,
                contract=contract,
                child_metadata={
                    "delegation_run_id": retry_run_id,
                    "delegation_task_id": retry_task_id,
                    "retry_of": task_id,
                },
                child_created_callback=created,
            )
            return {
                "delegation_run_id": retry_run_id,
                "task_id": retry_task_id,
                "child_session_id": result.session_id,
                "status": result.status.value,
            }
        except Exception as exc:
            service._store.update_delegation_task(
                retry_task_id, status="failed", error=str(exc),
            )
            service._store.complete_delegation_run(
                retry_run_id, status="failed",
            )
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router
