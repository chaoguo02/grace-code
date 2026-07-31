"""Public API for capability indexing and prompt context rendering."""

from capabilities.context import (
    build_capability_context,
    build_capability_sections,
    capability_sections_fingerprint,
    render_capability_sections,
    render_capability_selection,
    select_capability_sections,
)
from capabilities._compat import format_skills_for_prompt
from capabilities.render import (
    build_platform_info,
    build_tool_contract_rules,
    format_tool_descriptions,
)
from capabilities.index import CapabilityIndex
from capabilities.models import (
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityMetadata,
    CapabilityQuery,
    CapabilityRuntimeState,
    CapabilitySection,
    CapabilitySnapshot,
    CapabilityStatus,
)
from capabilities.render import CapabilityPromptRenderer

__all__ = [
    "CapabilityDescriptor",
    "CapabilityIndex",
    "CapabilityKind",
    "CapabilityMetadata",
    "CapabilityPromptRenderer",
    "CapabilityQuery",
    "CapabilityRuntimeState",
    "CapabilitySection",
    "CapabilitySnapshot",
    "CapabilityStatus",
    "build_capability_context",
    "build_capability_sections",
    "build_platform_info",
    "build_tool_contract_rules",
    "capability_sections_fingerprint",
    "format_skills_for_prompt",
    "format_tool_descriptions",
    "render_capability_sections",
    "render_capability_selection",
    "select_capability_sections",
]
