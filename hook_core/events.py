"""
CC-aligned hook events — all 16 lifecycle points.

Reference: https://code.claude.com/docs/en/hooks
"""

from __future__ import annotations

from enum import StrEnum


class HookEvent(StrEnum):
    """Hook lifecycle events aligned with Claude Code.

    Cadences:
      Per-session:  SessionStart, SessionEnd
      Per-turn:     UserPromptSubmit, Stop, StopFailure
      Per-tool:     PreToolUse, PostToolUse, PostToolUseFailure, PostToolBatch
    """

    # ── Tool execution (per-tool-call) ──────────────────────────────────
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    POST_TOOL_USE_FAILURE = "PostToolUseFailure"
    POST_TOOL_BATCH = "PostToolBatch"

    # ── Permissions ─────────────────────────────────────────────────────
    PERMISSION_REQUEST = "PermissionRequest"
    PERMISSION_DENIED = "PermissionDenied"

    # ── User input (per-turn) ───────────────────────────────────────────
    USER_PROMPT_SUBMIT = "UserPromptSubmit"

    # ── Model output (per-turn) ─────────────────────────────────────────
    STOP = "Stop"
    STOP_FAILURE = "StopFailure"

    # ── Session lifecycle ───────────────────────────────────────────────
    SESSION_START = "SessionStart"
    SESSION_END = "SessionEnd"

    # ── Subagents ───────────────────────────────────────────────────────
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"

    # ── Compaction ──────────────────────────────────────────────────────
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"

    # ── Notifications ───────────────────────────────────────────────────
    NOTIFICATION = "Notification"


# Events where hooks can BLOCK the operation.
# CC: PreToolUse, UserPromptSubmit, Stop, SubagentStop, PreCompact, PostToolBatch,
#     PermissionRequest, UserPromptExpansion, TaskCreated, TaskCompleted
BLOCKABLE_EVENTS: frozenset[HookEvent] = frozenset({
    HookEvent.PRE_TOOL_USE,
    HookEvent.PERMISSION_REQUEST,
    HookEvent.USER_PROMPT_SUBMIT,
    HookEvent.STOP,
    HookEvent.SUBAGENT_STOP,
    HookEvent.PRE_COMPACT,
    HookEvent.POST_TOOL_BATCH,
})
