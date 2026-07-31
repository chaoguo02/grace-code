"""Agent registry adapter for the capability index."""

from __future__ import annotations

from capabilities.models import (
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityMetadata,
    CapabilityQuery,
    CapabilityRuntimeState,
    CapabilityStatus,
)


class AgentCapabilityProvider:
    def __init__(self, agent_registry, parent_agent) -> None:
        self._agent_registry = agent_registry
        self._parent_agent = parent_agent

    def list_descriptors(self, query: CapabilityQuery) -> tuple[CapabilityDescriptor, ...]:
        if CapabilityKind.AGENT not in query.kinds:
            return ()
        if self._agent_registry is None or self._parent_agent is None:
            return ()

        descriptors: list[CapabilityDescriptor] = []
        for child in self._agent_registry.delegatable_by(self._parent_agent):
            descriptor = CapabilityDescriptor(
                metadata=CapabilityMetadata(
                    kind=CapabilityKind.AGENT,
                    name=str(child.name),
                    description=str(child.description or ""),
                    source="agent_registry",
                    namespace="agent",
                    invocation="Agent",
                    allowed_tools=tuple(sorted(getattr(child, "tools", frozenset()) or ())),
                    disallowed_tools=tuple(sorted(getattr(child, "disallowed_tools", frozenset()) or ())),
                    workspace_mode=str(getattr(child.workspace_mode, "value", child.workspace_mode)),
                    model=str(getattr(child, "model", "") or "inherit"),
                    effort=str(getattr(child, "effort", "") or "inherit"),
                    skills=tuple(getattr(child, "skills", ()) or ()),
                    mcp_servers=tuple(_mcp_server_names(getattr(child, "mcp_servers", ()) or ())),
                    delegation_scope=str(getattr(self._parent_agent.effective_delegation_scope, "value", self._parent_agent.effective_delegation_scope)),
                ),
                runtime=CapabilityRuntimeState(
                    status=CapabilityStatus.AVAILABLE,
                    visible_to_model=True,
                    activation="Agent",
                ),
            )
            if query.matches(descriptor):
                descriptors.append(descriptor)
        return tuple(descriptors)


def _mcp_server_names(entries) -> tuple[str, ...]:
    names: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict):
            names.extend(str(name) for name in entry.keys())
    return tuple(sorted(names))
