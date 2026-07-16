"""Artifact retrieval tools for evidence lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from context.artifacts import ArtifactStore
from tools.base import (
    BaseTool, ToolDependency, ToolEffect, ToolMetadata, ToolResult,
)


@dataclass
class ArtifactStoreRef:
    """Mutable reference used to bind tools to the active agent artifact store."""

    store: ArtifactStore | None = None


class ArtifactListTool(BaseTool):
    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.READ_AGENT_STATE}),
        dependency=ToolDependency.ARTIFACT_STORE,
    )
    def __init__(self, store_ref: ArtifactStoreRef) -> None:
        self._store_ref = store_ref

    @property
    def name(self) -> str:
        return "artifact_list"

    @property
    def description(self) -> str:
        return "List raw evidence artifacts captured during the current run."

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, params: dict[str, Any]) -> ToolResult:
        store = self._store_ref.store
        if store is None:
            return ToolResult(success=True, output="No artifact store is attached.")
        artifacts = store.list_artifacts()
        if not artifacts:
            return ToolResult(success=True, output="No artifacts captured yet.")
        lines = ["Artifacts:"]
        for artifact_id, tool_name, token_count in artifacts:
            lines.append(f"- {artifact_id} | {tool_name} | ~{token_count} tokens")
        return ToolResult(success=True, output="\n".join(lines))


class ArtifactReadTool(BaseTool):
    metadata = ToolMetadata(
        effects=frozenset({ToolEffect.READ_AGENT_STATE}),
        dependency=ToolDependency.ARTIFACT_STORE,
    )
    def __init__(self, store_ref: ArtifactStoreRef) -> None:
        self._store_ref = store_ref

    @property
    def name(self) -> str:
        return "artifact_read"

    @property
    def description(self) -> str:
        return "Read full raw evidence content by artifact_id from the current run."

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "description": "Artifact id such as art_ab12cd34."},
                "max_chars": {"type": "integer", "description": "Maximum characters to return.", "default": 8000},
            },
            "required": ["artifact_id"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        store = self._store_ref.store
        if store is None:
            return ToolResult(success=False, output="", error="No artifact store is attached.")
        artifact_id = str(params.get("artifact_id", "")).strip()
        if not artifact_id:
            return ToolResult(success=False, output="", error="artifact_id is required")
        content = store.get_full_content(artifact_id)
        if content is None:
            return ToolResult(success=False, output="", error=f"Artifact not found: {artifact_id}")
        max_chars = int(params.get("max_chars", 8000) or 8000)
        if max_chars > 0 and len(content) > max_chars:
            omitted = len(content) - max_chars
            content = f"{content[:max_chars]}\n... [{omitted} chars omitted from artifact {artifact_id}]"
        return ToolResult(success=True, output=content)
