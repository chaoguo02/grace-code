"""Display decisions derived from ToolMetadata.effects — NOT from tool name sets.

Claude Code comparison: Claude Code hardcodes per-tool display behavior.
Our approach is architecturally better — effects-based derivation means adding
a new read-only tool requires zero display-code changes.
"""

from __future__ import annotations

from tools.base import ToolEffect, ToolMetadata

# Tools whose output should be shown in compact/summarized form (not raw full output).
# These are read-only workspace-discovery tools — the LLM already processed the content.
_READ_WORKSPACE_EFFECTS = frozenset({ToolEffect.READ_WORKSPACE})


def is_display_silent(metadata: ToolMetadata) -> bool:
    """Tools that read/discover workspace show compact output — LLM already read it."""
    return bool(metadata.effects & _READ_WORKSPACE_EFFECTS) and ToolEffect.EXECUTE not in metadata.effects


def is_output_suppressed_in_history(metadata: ToolMetadata) -> bool:
    """Read-only file/content tools: output is redundant in history views."""
    return (
        bool(metadata.effects & _READ_WORKSPACE_EFFECTS)
        and ToolEffect.WRITE_WORKSPACE not in metadata.effects
        and ToolEffect.EXECUTE not in metadata.effects
    )


# Effects that produce output the user always wants to see inline (not externalized as artifact).
INLINE_EFFECTS = frozenset({
    ToolEffect.READ_WORKSPACE,
    ToolEffect.READ_VCS,
    ToolEffect.READ_AGENT_STATE,
})


def is_artifact_exempt(metadata: ToolMetadata) -> bool:
    """Read-only tools: output stays inline, never externalized to artifact storage."""
    return bool(metadata.effects & _INLINE_EFFECTS)
