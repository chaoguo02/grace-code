"""Skill registry adapter for the capability index."""

from __future__ import annotations

from capabilities.models import (
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityMetadata,
    CapabilityQuery,
    CapabilityRuntimeState,
    CapabilityStatus,
)


class SkillCapabilityProvider:
    def __init__(self, skill_registry, *, llm_invocable_only: bool = True) -> None:
        self._skill_registry = skill_registry
        self._llm_invocable_only = llm_invocable_only

    def list_descriptors(self, query: CapabilityQuery) -> tuple[CapabilityDescriptor, ...]:
        if CapabilityKind.SKILL not in query.kinds:
            return ()

        # Phase 2 Step 2.0: Read from ToolRegistry when attached.
        tool_registry = getattr(self._skill_registry, "_tool_registry", None)
        if tool_registry is not None:
            return self._list_from_tool_registry(query, tool_registry)

        # Fallback: legacy path (SkillRegistry._metadata dict).
        return self._list_from_metadata(query)

    def _list_from_tool_registry(
        self, query: CapabilityQuery, tool_registry: Any,
    ) -> tuple[CapabilityDescriptor, ...]:
        """Build descriptors from ToolRegistry._tools filtered by SkillActivationTool."""
        descriptors: list[CapabilityDescriptor] = []
        from skills.tool import SkillActivationTool
        for tool in getattr(tool_registry, "_tools", {}).values():
            if not isinstance(tool, SkillActivationTool):
                continue
            if not getattr(tool, "visible_to_llm", True):
                continue
            meta = tool._meta
            model_invocable = bool(getattr(meta, "model_invocable", True))
            if self._llm_invocable_only and not model_invocable:
                continue
            descriptor = CapabilityDescriptor(
                metadata=CapabilityMetadata(
                    kind=CapabilityKind.SKILL,
                    name=str(tool.name),
                    description=str(getattr(meta, "description", "") or ""),
                    source=str(getattr(meta, "source", "") or ""),
                    namespace="skill",
                    when_to_use=str(getattr(meta, "when_to_use", "") or ""),
                    invocation=tool.name,
                    path_scopes=tuple(getattr(meta, "paths", ()) or ()),
                    mcp_servers=tuple(sorted(getattr(meta, "mcp_servers", frozenset()) or ())),
                    allowed_tools=tuple(sorted(getattr(meta, "allowed_tools", frozenset()) or ())),
                    disallowed_tools=tuple(sorted(getattr(meta, "disallowed_tools", frozenset()) or ())),
                    user_invocable=bool(getattr(meta, "user_can_invoke", True)),
                    model_invocable=model_invocable,
                    file_path=str(getattr(meta, "file_path", "") or ""),
                    trusted=bool(getattr(meta, "trusted", True)),
                ),
                runtime=CapabilityRuntimeState(
                    status=CapabilityStatus.AVAILABLE,
                    visible_to_model=model_invocable,
                    activation="Skill",
                ),
            )
            if query.matches(descriptor):
                descriptors.append(descriptor)
        return tuple(descriptors)

    def _list_from_metadata(
        self, query: CapabilityQuery,
    ) -> tuple[CapabilityDescriptor, ...]:
        """Build descriptors from SkillRegistry._metadata dict (legacy path)."""
        descriptors: list[CapabilityDescriptor] = []
        for invocation_name, metadata in self._skill_registry.list_skill_entries():
            model_invocable = bool(getattr(metadata, "model_invocable", True))
            if self._llm_invocable_only and not model_invocable:
                continue
            descriptor = CapabilityDescriptor(
                metadata=CapabilityMetadata(
                    kind=CapabilityKind.SKILL,
                    name=str(invocation_name),
                    description=str(getattr(metadata, "description", "") or ""),
                    source=str(getattr(metadata, "source", "") or ""),
                    namespace="skill",
                    when_to_use=str(getattr(metadata, "when_to_use", "") or ""),
                    invocation="Skill",
                    path_scopes=tuple(getattr(metadata, "paths", ()) or ()),
                    mcp_servers=tuple(sorted(getattr(metadata, "mcp_servers", frozenset()) or ())),
                    allowed_tools=tuple(sorted(getattr(metadata, "allowed_tools", frozenset()) or ())),
                    disallowed_tools=tuple(sorted(getattr(metadata, "disallowed_tools", frozenset()) or ())),
                    user_invocable=bool(getattr(metadata, "user_can_invoke", True)),
                    model_invocable=model_invocable,
                    file_path=str(getattr(metadata, "file_path", "") or ""),
                    trusted=bool(getattr(metadata, "trusted", True)),
                ),
                runtime=CapabilityRuntimeState(
                    status=CapabilityStatus.AVAILABLE,
                    visible_to_model=model_invocable,
                    activation="Skill",
                ),
            )
            if query.matches(descriptor):
                descriptors.append(descriptor)
        return tuple(descriptors)
