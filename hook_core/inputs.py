"""
G11: Hook typed inputs — FrozenJsonObject, no untyped dict, no legacy context.

Each lifecycle point has its own frozen input class.
- tool_input uses FrozenJsonObject
- Stop uses OutcomeCandidate summary (not full mutable messages)
- Zero Any imports in this module
"""

from __future__ import annotations

from dataclasses import dataclass

from core.json_values import FrozenJsonObject, freeze_json


# ── Tool execution ──────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PreToolUseInput:
    tool_name: str
    tool_input: FrozenJsonObject  # G11: was dict[str, Any]
    session_id: str = ""
    tool_use_id: str = ""
    cwd: str = ""


@dataclass(frozen=True, slots=True)
class PostToolUseInput:
    tool_name: str
    tool_input: FrozenJsonObject  # G11: was dict[str, Any]
    tool_output: str = ""
    session_id: str = ""
    tool_use_id: str = ""
    success: bool = True


@dataclass(frozen=True, slots=True)
class PostToolUseFailureInput:
    tool_name: str
    tool_input: FrozenJsonObject  # G11: was dict[str, Any]
    error_message: str = ""
    error_type: str = ""
    session_id: str = ""
    tool_use_id: str = ""


@dataclass(frozen=True, slots=True)
class PostToolBatchInput:
    session_id: str = ""
    tool_count: int = 0


# ── Permissions ─────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PermissionRequestInput:
    tool_name: str
    tool_input: FrozenJsonObject  # G11: was dict[str, Any]
    required_permissions: frozenset[str] = frozenset()
    session_id: str = ""
    agent_id: str = ""


@dataclass(frozen=True, slots=True)
class PermissionDeniedInput:
    tool_name: str
    tool_input: FrozenJsonObject  # G11: was dict[str, Any]
    session_id: str = ""


# ── User input ──────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class UserPromptSubmitInput:
    prompt: str
    session_id: str = ""


# ── Model output ────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class StopInput:
    """G11: Uses summary fields, NOT full mutable message list."""
    session_id: str = ""
    stop_hook_active: bool = False
    last_assistant_message: str = ""
    steps_taken: int = 0
    tokens_used: int = 0
    agent_id: str = ""
    agent_type: str = ""
    outcome_summary: str = ""  # G11: replaced messages: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class StopFailureInput:
    session_id: str = ""
    error_type: str = ""


# ── Session lifecycle ───────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SessionStartInput:
    session_id: str = ""
    agent_type: str = ""
    source: str = ""


@dataclass(frozen=True, slots=True)
class SessionEndInput:
    session_id: str = ""
    reason: str = ""


# ── Subagents ───────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SubagentStartInput:
    session_id: str = ""
    agent_id: str = ""
    agent_type: str = ""


@dataclass(frozen=True, slots=True)
class SubagentStopInput:
    session_id: str = ""
    agent_id: str = ""
    agent_name: str = ""
    steps_taken: int = 0


# ── Compaction ──────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PreCompactInput:
    session_id: str = ""
    tokens_before: int = 0
    trigger: str = ""


@dataclass(frozen=True, slots=True)
class PostCompactInput:
    session_id: str = ""
    tokens_after: int = 0


# ── T7: PostToolBatch ───────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PostToolBatchInput:
    session_id: str = ""
    tool_count: int = 0


# ── Notifications ───────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class NotificationInput:
    session_id: str = ""
    message: str = ""
    level: str = ""
