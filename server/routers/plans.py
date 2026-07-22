"""
Plans router — plan library browser.

Mounted under ``/api/plans``.
Lists all plan files from ``.grace/plans/`` with session-enriched metadata.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

logger = logging.getLogger(__name__)


def create_plans_router(get_service: Any) -> APIRouter:
    """Create the plans router with dependency injection."""
    router = APIRouter(prefix="/api/plans", tags=["plans"])

    @router.get("")
    async def list_plans(
        limit: int = 50,
        offset: int = 0,
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """
        List all plan files in .grace/plans/ enriched with session metadata.

        **Query Parameters:**
        - ``limit`` (int, default 50): Max plans to return.
        - ``offset`` (int, default 0): Pagination offset.

        **Response (200):**
        - ``plans`` (array): Plan entries with filename, session_id, title,
          preview, created_at, size, status.
        - ``total`` (int): Total plan files found.
        """
        plan_dir = Path(service.repo_path) / ".grace" / "plans"
        if not plan_dir.is_dir():
            return {"plans": [], "total": 0}

        # Collect all .md files with their metadata
        entries: list[dict[str, Any]] = []
        for plan_file in sorted(plan_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                stat = plan_file.stat()
                raw_name = plan_file.stem  # filename without .md

                # Try to read content for preview and YAML frontmatter
                content = ""
                goal = ""
                try:
                    content = plan_file.read_text(encoding="utf-8")
                except Exception:
                    pass

                # Extract frontmatter goal if present
                preview = content[:200] if content else ""
                if content.startswith("---"):
                    try:
                        end = content.index("---", 3)
                        frontmatter = content[3:end].strip()
                        for line in frontmatter.split("\n"):
                            if line.startswith("goal:"):
                                goal = line.split(":", 1)[1].strip()
                                break
                        # Preview starts after frontmatter
                        preview = content[end + 3:].strip()[:200]
                    except (ValueError, IndexError):
                        pass

                # Try to match filename to a session
                session_info: dict[str, Any] | None = None
                try:
                    rec = service.session_service.get_session(raw_name)
                    if rec is not None:
                        session_info = {
                            "id": rec.id,
                            "agent_name": rec.agent_name,
                            "title": rec.title,
                            "status": rec.status.value if hasattr(rec.status, "value") else str(rec.status),
                            "summary": rec.summary[:200] if rec.summary else "",
                        }
                except Exception:
                    pass

                entries.append({
                    "filename": plan_file.name,
                    "session_id": raw_name if session_info else None,
                    "title": goal or (session_info["title"] if session_info else raw_name),
                    "preview": preview[:200],
                    "content": content,
                    "size_bytes": stat.st_size,
                    "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    "session": session_info,
                })
            except OSError:
                continue

        total = len(entries)
        return {
            "plans": entries[offset:offset + limit],
            "total": total,
            "has_more": (offset + limit) < total,
        }

    @router.get("/{filename:path}")
    async def get_plan(
        filename: str,
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """
        Get full content of a plan file by filename.

        **Path Parameters:**
        - ``filename`` (string): Plan filename, e.g. ``abc123.md``.

        **Response (200):**
        - ``filename``, ``content``, ``session_id``, ``created_at``, ``size_bytes``.
        """
        plan_dir = Path(service.repo_path) / ".grace" / "plans"
        plan_file = plan_dir / filename
        if not plan_file.is_file():
            raise HTTPException(status_code=404, detail=f"Plan file not found: {filename}")

        try:
            content = plan_file.read_text(encoding="utf-8")
            stat = plan_file.stat()
            raw_name = plan_file.stem

            session_info = None
            try:
                rec = service.session_service.get_session(raw_name)
                if rec is not None:
                    session_info = {
                        "id": rec.id,
                        "agent_name": rec.agent_name,
                        "title": rec.title,
                        "status": rec.status.value if hasattr(rec.status, "value") else str(rec.status),
                    }
            except Exception:
                pass

            return {
                "filename": plan_file.name,
                "session_id": raw_name if session_info else None,
                "content": content,
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "session": session_info,
            }
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.patch("/{filename:path}")
    async def update_plan(
        filename: str,
        body: dict[str, Any],
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """
        Update a plan file's content.

        **Request Body:**
        - ``content`` (string, required): New plan markdown content.

        **Response (200):**
        - ``filename``, ``updated`` (bool), ``size_bytes``.
        """
        plan_dir = Path(service.repo_path) / ".grace" / "plans"
        plan_file = plan_dir / filename
        if not plan_file.is_file():
            raise HTTPException(status_code=404, detail=f"Plan file not found: {filename}")

        content = body.get("content")
        if content is None:
            raise HTTPException(status_code=422, detail="Missing required field: content")

        # Path traversal guard
        try:
            plan_file.resolve().relative_to(plan_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid filename")

        try:
            plan_file.write_text(str(content), encoding="utf-8")
            stat = plan_file.stat()
            return {
                "filename": plan_file.name,
                "updated": True,
                "size_bytes": stat.st_size,
            }
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @router.delete("/{filename:path}")
    async def delete_plan(
        filename: str,
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """
        Delete a plan file permanently.

        **Response (200):**
        - ``filename``, ``deleted`` (bool).
        """
        plan_dir = Path(service.repo_path) / ".grace" / "plans"
        plan_file = plan_dir / filename
        if not plan_file.is_file():
            raise HTTPException(status_code=404, detail=f"Plan file not found: {filename}")

        # Path traversal guard
        try:
            plan_file.resolve().relative_to(plan_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid filename")

        try:
            plan_file.unlink()
            return {"filename": plan_file.name, "deleted": True}
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    return router
