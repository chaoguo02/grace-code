"""SkillActivationService — single entry point for skill activation evidence.

All 4 skill entry paths converge here:
  - tool_call (SkillTool in skills/tool.py)
  - http_request (agent_service.py resolve_user_skill)
  - cli_slash (entry/chat.py _handle_slash_skill)
  - preload (runtime_prompt_builder.py _load_skills)

The service resolves skill metadata, computes a fingerprint, and
produces an activation record.  It does NOT record evidence directly —
callers are responsible for flushing through the RunEvidenceStore.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SkillActivation:
    """Resolved facts about one skill activation."""

    skill_name: str
    source: str  # tool_call | http_request | cli_slash | preload
    fingerprint: str = ""
    mcp_dependencies: tuple[str, ...] = ()
    session_id: str = ""


class SkillActivationService:
    """Resolve skill metadata and produce activation records.

    Does NOT own the evidence store — callers handle persistence.
    """

    def __init__(self, skill_registry: Any = None) -> None:
        self._registry = skill_registry

    def activate(
        self,
        skill_name: str,
        *,
        source: str,
        session_id: str = "",
    ) -> SkillActivation | None:
        """Resolve skill metadata and return an activation record.

        Returns None if the skill is not found.
        """
        if self._registry is None:
            return SkillActivation(
                skill_name=skill_name, source=source,
                session_id=session_id,
            )

        meta = None
        try:
            meta = self._registry.get_skill_meta(skill_name)
        except Exception:
            pass

        fingerprint = ""
        mcp_deps: tuple[str, ...] = ()
        if meta is not None:
            fingerprint = skill_fingerprint(meta)
            if getattr(meta, "mcp_servers", None):
                mcp_deps = tuple(sorted(meta.mcp_servers))

        return SkillActivation(
            skill_name=skill_name,
            source=source,
            fingerprint=fingerprint,
            mcp_dependencies=mcp_deps,
            session_id=session_id,
        )


def skill_fingerprint(meta) -> str:
    import hashlib
    from pathlib import Path

    file_path = str(getattr(meta, "file_path", "") or "")
    content = ""
    if file_path:
        try:
            content = Path(file_path).read_text(encoding="utf-8")
        except OSError:
            content = ""
    seed = (
        f"{meta.name}|{getattr(meta, 'source', '')}|"
        f"{sorted(getattr(meta, 'mcp_servers', []))}|"
        f"{sorted(getattr(meta, 'allowed_tools', []))}|"
        f"{file_path}|{content}"
    )
    return hashlib.sha256(seed.encode()).hexdigest()[:16]
