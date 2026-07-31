"""Pure data models for prompt-facing capability indexing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum


class CapabilityKind(str, Enum):
    TOOL = "tool"
    SKILL = "skill"
    MCP_SERVER = "mcp_server"
    MCP_TOOL = "mcp_tool"
    AGENT = "agent"


class CapabilityStatus(str, Enum):
    AVAILABLE = "available"
    DEFERRED = "deferred"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    HIDDEN = "hidden"


@dataclass(frozen=True)
class CapabilityMetadata:
    kind: CapabilityKind
    name: str
    description: str = ""
    source: str = ""
    namespace: str = ""
    when_to_use: str = ""
    invocation: str = ""
    server_name: str = ""
    path_scopes: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    workspace_mode: str = ""
    model: str = ""
    effort: str = ""
    skills: tuple[str, ...] = ()
    delegation_scope: str = ""
    user_invocable: bool = True
    model_invocable: bool = True
    file_path: str = ""
    trusted: bool = True


@dataclass(frozen=True)
class CapabilityRuntimeState:
    status: CapabilityStatus = CapabilityStatus.AVAILABLE
    visible_to_model: bool = True
    activation: str = ""
    reason: str = ""
    error: str = ""


@dataclass(frozen=True)
class CapabilityDescriptor:
    metadata: CapabilityMetadata
    runtime: CapabilityRuntimeState = CapabilityRuntimeState()

    def fingerprint_key(self) -> tuple:
        error_hash = (
            hashlib.sha256(self.runtime.error.encode("utf-8")).hexdigest()[:8]
            if self.runtime.error else ""
        )
        return (
            self.metadata.kind.value,
            self.metadata.name,
            self.metadata.namespace,
            self.metadata.description,
            self.metadata.when_to_use,
            self.metadata.invocation,
            self.metadata.server_name,
            tuple(sorted(self.metadata.path_scopes)),
            tuple(sorted(self.metadata.mcp_servers)),
            tuple(sorted(self.metadata.allowed_tools)),
            tuple(sorted(self.metadata.disallowed_tools)),
            self.metadata.workspace_mode,
            self.metadata.model,
            self.metadata.effort,
            tuple(sorted(self.metadata.skills)),
            self.metadata.delegation_scope,
            self.metadata.user_invocable,
            self.metadata.model_invocable,
            self.runtime.status.value,
            self.runtime.visible_to_model,
            self.runtime.activation,
            self.runtime.reason,
            error_hash,
        )


@dataclass(frozen=True)
class CapabilitySnapshot:
    descriptors: tuple[CapabilityDescriptor, ...]
    fingerprint: str

    def by_kind(self, kind: CapabilityKind) -> tuple[CapabilityDescriptor, ...]:
        return tuple(
            descriptor for descriptor in self.descriptors
            if descriptor.metadata.kind is kind
        )

    @staticmethod
    def fingerprint_for(descriptors: tuple[CapabilityDescriptor, ...]) -> str:
        ordered = sorted(descriptors, key=lambda descriptor: descriptor.fingerprint_key())
        keys = [_fingerprint_json_key(descriptor.fingerprint_key()) for descriptor in ordered]
        raw = json.dumps(keys, ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _fingerprint_json_key(key: tuple) -> list:
    """Convert a ``fingerprint_key()`` tuple to a JSON-serialisable list.

    Inner tuples are converted to sorted lists so the JSON output is a
    valid, deterministic array of arrays — no reliance on ``default=str``
    or Python ``tuple.__repr__``.
    """
    return [list(sorted(v)) if isinstance(v, tuple) else v for v in key]


@dataclass(frozen=True)
class CapabilityQuery:
    kinds: frozenset[CapabilityKind] = frozenset(CapabilityKind)
    excluded_statuses: frozenset[CapabilityStatus] = frozenset({CapabilityStatus.HIDDEN})
    visible_to_model: bool | None = True
    namespaces: frozenset[str] | None = None
    parent_agent: str | None = None

    def matches(self, descriptor: CapabilityDescriptor) -> bool:
        if descriptor.metadata.kind not in self.kinds:
            return False
        if descriptor.runtime.status in self.excluded_statuses:
            return False
        if self.visible_to_model is not None and descriptor.runtime.visible_to_model is not self.visible_to_model:
            return False
        if self.namespaces is not None and descriptor.metadata.namespace not in self.namespaces:
            return False
        return True


@dataclass(frozen=True)
class CapabilitySection:
    title: str
    content: str
    priority: int
    token_estimate: int
    kind_filter: CapabilityKind
    descriptor_count: int = 0
    source_fingerprint: str = ""
