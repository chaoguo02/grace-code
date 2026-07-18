"""
Attachments router — file upload for session context.

Mounted under ``/api/sessions/{session_id}/attachments``.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File

logger = logging.getLogger(__name__)


def create_attachments_router(get_service: Any) -> APIRouter:
    """Create the attachments router with dependency injection."""
    router = APIRouter(prefix="/api/sessions", tags=["attachments"])

    @router.post("/{session_id}/attachments")
    async def upload_attachment(
        session_id: str,
        file: UploadFile = File(...),
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """
        Upload a file attachment for a session.

        The file is saved to the session's attachment directory on disk.
        Attached files are available to the agent via the Read tool.

        **Path Parameters:**
        - ``session_id`` (string): 12-char hex session ID.

        **Request Body:**
        - ``file`` (multipart/form-data): The file to upload.

        **Response (200):**
        - ``attachment_id`` (string): Unique attachment identifier.
        - ``filename`` (string): Original filename.
        - ``size`` (int): File size in bytes.
        - ``path`` (string): Relative path within the session's attachment dir.

        **Errors:**
        - 404: Session not found.
        - 413: File too large (>10 MB).
        """
        # Validate session
        rec = service.session_service.get_session(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

        # Validate file
        if not file.filename:
            raise HTTPException(status_code=400, detail="No filename provided")

        MAX_SIZE = 10 * 1024 * 1024  # 10 MB

        # Read content (with size check)
        content = await file.read()
        if len(content) > MAX_SIZE:
            raise HTTPException(status_code=413, detail="File too large (max 10 MB)")

        # Create attachment directory
        from core.state_paths import ProjectStatePaths
        state = ProjectStatePaths.for_project(service.repo_path)
        attach_dir = state.root / "attachments" / session_id
        attach_dir.mkdir(parents=True, exist_ok=True)

        # Save file with timestamp prefix to avoid name collisions
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        safe_name = f"{ts}_{file.filename}"
        dest = attach_dir / safe_name
        dest.write_bytes(content)

        logger.info("Attachment saved: %s (%d bytes) for session %s", dest, len(content), session_id)

        return {
            "attachment_id": safe_name,
            "filename": file.filename,
            "size": len(content),
            "path": str(dest.relative_to(state.root)),
        }

    return router
