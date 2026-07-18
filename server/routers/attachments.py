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

    # ── POST /api/sessions/{session_id}/attachments/resolve ──────────────

    @router.post("/{session_id}/attachments/resolve")
    async def resolve_attachment(
        session_id: str,
        body: dict[str, Any],
        service=Depends(get_service),
    ) -> dict[str, Any]:
        """Resolve an @mention path to file content.

        Supports:
        - ``@path/to/file`` → full file content (capped at 10K chars)
        - ``@path/to/dir/`` → directory listing
        - ``@*.py`` → glob match results

        The resolved content is injected into the conversation context
        before the agent sees the prompt.
        """
        rec = service.session_service.get_session(session_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

        path = (body.get("path") or "").strip()
        if not path:
            raise HTTPException(status_code=400, detail="'path' is required")

        repo = Path(service.repo_path).resolve()
        full_path = (repo / path).resolve()

        # Security: path must be inside repo
        try:
            full_path.relative_to(repo)
        except ValueError:
            raise HTTPException(status_code=403, detail="Path outside repository")

        if not full_path.exists():
            # Try glob
            import glob as glob_mod
            pattern = str(repo / path)
            matches = [str(Path(m).relative_to(repo)) for m in glob_mod.glob(pattern, recursive=True)]
            if matches:
                return {
                    "type": "glob",
                    "path": path,
                    "matches": matches[:20],
                }
            raise HTTPException(status_code=404, detail=f"Path not found: {path}")

        if full_path.is_file():
            MAX_CHARS = 10000
            try:
                content = full_path.read_text(encoding="utf-8")
                truncated = len(content) > MAX_CHARS
                return {
                    "type": "file",
                    "path": path,
                    "content": content[:MAX_CHARS],
                    "lines": content.count("\n") + 1,
                    "truncated": truncated,
                }
            except UnicodeDecodeError:
                return {"type": "file", "path": path, "content": "[Binary file — not displayable]", "lines": 0}

        if full_path.is_dir():
            files = [
                str(p.relative_to(full_path))
                for p in sorted(full_path.iterdir())
                if not p.name.startswith(".")
            ]
            return {"type": "directory", "path": path, "files": files[:50]}

        raise HTTPException(status_code=404, detail=f"Unsupported path type: {path}")

    return router
