"""Subagent registry factory — builds restricted child tool registries.

Extracted from fork_subagent().
Constitution: subagent.py should "run subagents", not assemble tool registries.
Allowed/disallowed tool resolution, SubmitFindings injection, and policy
wrapping belong here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.policy import PhasePolicy
    from agent.v2.models import AgentDefinition
    from tools.base import ToolRegistry

logger = logging.getLogger(__name__)


def build_restricted_registry(
    definition: "AgentDefinition",
    base_registry: "ToolRegistry",
    *,
    repo_path: str,
    parent_policy: "PhasePolicy",
) -> tuple["ToolRegistry", Any]:
    """Build a permission-scoped tool registry for a child subagent.

    Resolves both allowed and disallowed tools through alias resolution.
    Injects SubmitFindingsTool for structured output. Wraps in policy layer.

    Returns (wrapped_registry, findings_accumulator). The caller uses the
    accumulator to collect structured findings after the subagent completes.

    Args:
        definition: The agent definition (allowed/disallowed tools, etc.)
        base_registry: The parent's full tool registry
        repo_path: Working directory (for policy wrapper)
    """
    from agent.v2.agent_registry import resolve_tool_set
    from agent.policy_registry import PolicyAwareToolRegistry
    from agent.policy import PhasePolicy
    from tools.base import ExecutionContext

    # `definition` is the dispatch-time fact source.  Re-discovering an agent
    # by name here can resolve a different project/CWD definition and silently
    # change its authority between selection and execution.
    allowed_tools = resolve_tool_set(definition.tools)
    disallowed = resolve_tool_set(definition.disallowed_tools)
    final_tools = allowed_tools - disallowed
    findings_required = (
        "submit_findings" in final_tools
        or "submit_findings" in resolve_tool_set(definition.required_tools)
        or definition.completion_requires.get("submit_findings", 0) > 0
    )
    if disallowed:
        logger.debug(
            "Subagent '%s': allowed=%d tools, disallowed=%d (resolved: %s), final=%d",
            definition.name, len(allowed_tools), len(disallowed),
            sorted(disallowed), len(final_tools),
        )

    restricted_registry = base_registry.scoped(ExecutionContext(
        workspace_root=repo_path,
        repo_path=repo_path,
    )).filtered(final_tools - {"submit_findings"})

    # ── Structured findings tool (fresh accumulator per subagent) ──
    from tools.submit_findings_tool import FindingsAccumulator, SubmitFindingsTool
    findings_accumulator = FindingsAccumulator()
    if findings_required:
        submit_findings_tool = SubmitFindingsTool(
            repo_path=repo_path,
            accumulator=findings_accumulator,
        )
        restricted_registry.register(submit_findings_tool)

    # Phase-policy wrap
    wrapped_registry = PolicyAwareToolRegistry(
        base=restricted_registry,
        phase_policy=parent_policy.with_allowed_tools(
            frozenset(restricted_registry.tool_names)
        ),
        repo_path=repo_path,
        phase_name=f"fork-{definition.name}",
    )
    return wrapped_registry, findings_accumulator
