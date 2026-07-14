"""Observation rendering — formats tool results into model-visible text.

Extracted from ReActAgent._build_tool_result_content(),
_truncate_output(), and _format_observations_for_history().

Constitution: this is "observation content formatting" — it belongs in agent/
but NOT in the main loop. The main loop should call these pure functions
without owning the formatting logic.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tools.base import ToolRole

if TYPE_CHECKING:
    from agent.task import Observation

logger = logging.getLogger(__name__)

# Tools whose output should NOT be artifacted (always inline)
ARTIFACT_EXEMPT_TOOLS = frozenset({
    "file_read", "file_view", "file_edit", "file_write",
    "find_files", "find_symbol",
    "git_status", "git_add", "git_commit",
    "memory_read", "memory_list", "memory_search",
})


def truncate_output(text: str, max_chars: int = 8000) -> str:
    """Pre-truncate oversized tool output, keeping head + tail."""
    if len(text) <= max_chars:
        return text
    keep = max_chars // 2
    head = text[:keep]
    tail = text[-keep:]
    omitted = len(text) - max_chars
    return f"{head}\n\n... [{omitted} chars omitted] ...\n\n{tail}"


def build_tool_result_content(
    observation: "Observation",
    *,
    artifact_store=None,
    exempt_tools: frozenset = ARTIFACT_EXEMPT_TOOLS,
    tool_roles: "frozenset[ToolRole]" = frozenset(),
) -> str:
    """Build tool result content for native tool_use mode (no [Tool:] wrapper).

    Args:
        observation: The tool observation to format
        artifact_store: Optional ArtifactStore for large-output offloading
        exempt_tools: Tool names whose output is always inline
    """
    if ToolRole.DELEGATE in tool_roles:
        output = observation.output or ""
        if observation.error:
            output += f"\n<error>{observation.error}</error>"
        return output or "(no output)"

    parts: list[str] = []
    if observation.output:
        output = observation.output
        if (
            observation.tool_name not in exempt_tools
            and artifact_store is not None
        ):
            output, was_stored = artifact_store.maybe_store(
                observation.tool_name, output
            )
            if was_stored:
                logger.debug(
                    "Artifacted output from %s (%d tokens stored)",
                    observation.tool_name,
                    artifact_store.total_tokens_stored,
                )
        else:
            output = truncate_output(output)
        parts.append(output)
    if observation.error and not observation.is_success():
        parts.append(f"Error: {observation.error}")
    return "\n".join(parts) if parts else "(no output)"


def format_observations_for_history(
    observations: list["Observation"],
    *,
    artifact_store=None,
    exempt_tools: frozenset = ARTIFACT_EXEMPT_TOOLS,
    roles_by_tool: "dict[str, frozenset[ToolRole]] | None" = None,
) -> str:
    """Format multiple observations as one user message (parallel tool_calls, text fallback)."""
    lines = []
    for obs in observations:
        status = "SUCCESS" if obs.is_success() else "ERROR"
        lines.append(f"[Tool: {obs.tool_name} | {status}]")
        roles = (roles_by_tool or {}).get(obs.tool_name, frozenset())
        if ToolRole.DELEGATE in roles:
            lines.append(build_tool_result_content(
                obs,
                artifact_store=artifact_store,
                exempt_tools=exempt_tools,
                tool_roles=roles,
            ))
            continue
        if obs.output:
            output = obs.output
            if (
                obs.tool_name not in exempt_tools
                and artifact_store is not None
            ):
                output, _ = artifact_store.maybe_store(obs.tool_name, output)
            else:
                output = truncate_output(output)
            lines.append(output)
        if obs.error and not obs.is_success():
            lines.append(f"Error: {obs.error}")
    return "\n".join(lines)
