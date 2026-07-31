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
