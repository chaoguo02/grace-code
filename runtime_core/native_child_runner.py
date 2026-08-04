"""runtime_core/native_child_runner.py

Phase 1: Native Child Runner — fresh child execution on the Native path.

Four pure functions that together enable NativeStepLoop to launch a child:
  1. filter_tool_schemas        — parent schemas → child-visible subset
  2. build_child_conversation   — AgentDefinition + prompt → NativeConversation
  3. child_runtime_ports        — parent ports + definition → filtered child ports
  4. run_native_child           — AgentRuntime.run() thin wrapper

All functions are stateless.  No legacy imports (no SessionRuntime, no LLMMessage,
no ReActAgent).  Operates entirely on NativeBackend / NativeMessage / RuntimePorts.
"""

from __future__ import annotations

import time as _time
import uuid as _uuid
from typing import TYPE_CHECKING, Callable

from core.eventing.identifiers import SessionId, RunId
from runtime_core.execution import (
    CancellationHandle,
    ConversationSnapshot,
    RuntimeExecution,
)
from runtime_core.native_backend import NativeToolSchema
from runtime_core.native_message import NativeConversation, NativeMessage
from runtime_core.outcome import RuntimeOutcome
from runtime_core.ports import RuntimePorts, ToolPort

if TYPE_CHECKING:
    from agent.session.models import AgentDefinition


# ── Subagent protocol rules (CC-aligned) ─────────────────────────────────────
# Equivalent to _SUBAGENT_SUMMARY_RULE in task_tool.py and _SUBAGENT_SUMMARY_RULE
# in subagent.py, but formatted for NativeMessage injection.

_SUBAGENT_PROTOCOL = """[SUBAGENT PROTOCOL — CC-aligned]
Your final message IS your return value to the parent. The parent sees ONLY your
final message — not your reasoning, not your tool history, not your intermediate
thoughts. Make it standalone and directly usable.

OUTPUT: State what you found/did concisely (~1K-2K tokens). If you could NOT
complete: say exactly what's missing. Label unverified claims as "[unverified]".

TOOLS: Use Read/Grep/Glob BEFORE shell. Shell is ONLY for tests, builds, git.
NEVER use shell to read/search files.

BOUNDARIES: Stay within scope. Only report with concrete evidence (file paths,
line numbers). Do NOT edit unless explicitly asked. If no Write/Edit tools:
you are READ-ONLY."""


# ── 1. filter_tool_schemas ────────────────────────────────────────────────────


def filter_tool_schemas(
    parent_schemas: tuple[NativeToolSchema, ...],
    allowed: frozenset[str],
    disallowed: frozenset[str],
) -> tuple[NativeToolSchema, ...]:
    """Child-visible tools = parent ∩ allowed − disallowed.

    CC semantics:
      - ``tools`` (allowlist): only these parent tools are visible to child
      - ``disallowedTools`` (denylist): these are removed even if allowlisted
      - empty ``allowed`` = no tools visible (not "inherit all")

    If ``allowed`` is empty, returns empty tuple — child has no tools.
    This matches CC behavior: omitting ``tools`` inherits all; specifying
    ``tools`` (even as empty string) restricts to exactly that set.
    Grace Code represents "inherit all" by not calling filter_tool_schemas at all.
    """
    return tuple(
        s for s in parent_schemas
        if s.name in allowed and s.name not in disallowed
    )


# ── 2. build_child_conversation ───────────────────────────────────────────────


def build_child_conversation(
    definition: "AgentDefinition",
    prompt: str,
    description: str = "",
    *,
    project_dir: str = "",
) -> NativeConversation:
    """Build the initial NativeConversation for a fresh child — CC-aligned.

    Message order:
      1. System prompt (AgentDefinition.system_prompt — replaces default)
      2. Project rules (.grace/GRACE.md — CC CLAUDE.md equivalent, Phase 3)
      3. Subagent protocol rules (runtime-injected behavior constraints)
      4. User turn: task description + prompt
    """
    messages: list[NativeMessage] = []

    # 1. System prompt — agent body REPLACES default (CC priority chain)
    system_text = getattr(definition, "system_prompt", "") or ""
    if system_text.strip():
        messages.append(NativeMessage.system(system_text.strip()))

    # 2. Project rules — CC CLAUDE.md <system-reminder> mechanism (Phase 3)
    if project_dir:
        from runtime_core.native_child_context import _load_project_rules
        rules = _load_project_rules(project_dir)
        if rules:
            messages.append(NativeMessage.system(rules))

    # 3. Subagent protocol
    messages.append(NativeMessage.user(_SUBAGENT_PROTOCOL))

    # 4. User turn: description + prompt
    task = _format_task_message(description, prompt)
    messages.append(NativeMessage.user(task))

    return NativeConversation.from_messages(messages)


def _format_task_message(description: str, prompt: str) -> str:
    """Format the user-facing task message for the child."""
    if description.strip():
        return f"[TASK] {description.strip()}\n\n{prompt.strip()}"
    return prompt.strip()


# ── 3. child_runtime_ports ────────────────────────────────────────────────────


class _ScopedToolPort:
    """ToolPort adapter that enforces a per-child tool allowlist.

    CC semantics: child tools = parent tools ∩ definition.tools − definition.disallowedTools.
    Tools not in the allowlist return ToolDenied.
    """

    def __init__(self, parent: ToolPort, allowed: frozenset[str]) -> None:
        self._parent = parent
        self._allowed = allowed

    def execute(self, tool_name: str, params, invocation_id: str = "") -> object:
        from runtime_core.ports import ToolDenied
        if tool_name not in self._allowed:
            return ToolDenied(
                tool_name=tool_name,
                reason=f"Tool '{tool_name}' is not allowed for this subagent",
            )
        return self._parent.execute(tool_name, params, invocation_id)


def child_runtime_ports(
    parent_ports: RuntimePorts,
    definition: "AgentDefinition",
) -> RuntimePorts:
    """Construct child-scoped RuntimePorts with restricted tool set.

    Tool filtering: parent ∩ definition.tools − definition.disallowedTools.
    All other ports (llm, hooks, live_events, clock, token_usage) are
    shared with the parent — only tools are scoped.
    """
    allowed = getattr(definition, "tools", frozenset()) or frozenset()
    disallowed = getattr(definition, "disallowed_tools", frozenset()) or frozenset()
    effective = allowed - disallowed

    return RuntimePorts(
        llm=parent_ports.llm,
        tools=_ScopedToolPort(parent_ports.tools, effective),
        hooks=parent_ports.hooks,
        live_events=parent_ports.live_events,
        clock=parent_ports.clock,
        token_usage=parent_ports.token_usage,
    )


# ── 4. run_native_child ───────────────────────────────────────────────────────


def run_native_child(
    ports: RuntimePorts,
    session_id: SessionId,
    run_id: RunId,
    conversation: NativeConversation,
    cancellation: CancellationHandle,
    max_steps: int,
    budget_tokens: int,
) -> RuntimeOutcome:
    """Execute one child run on the Native path.

    Thin wrapper around AgentRuntime.run(RuntimeExecution(...)).
    Functionally equivalent to legacy run_child_agent() but uses
    NativeStepLoop instead of ReActAgent.

    The conversation (NativeConversation) is converted to API dicts
    for ConversationSnapshot.  For fresh children this is safe because
    the conversation contains only text system/user messages — no
    structured tool_use/tool_result blocks.
    """
    started = _time.monotonic()

    # Convert NativeConversation → ConversationSnapshot.messages
    # NativeMessage.to_api_dict() produces {"role": ..., "content": ...} dicts.
    # For system messages with only TextBlock content, content is a string.
    msg_dicts: list[dict] = []
    for msg in conversation.messages:
        d = msg.to_api_dict()
        # Flatten TextBlock-only content to plain string for ConversationSnapshot
        content = d.get("content", "")
        if isinstance(content, list):
            # Extract text from TextBlock list
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            content = "\n".join(parts) if parts else ""
        msg_dicts.append({"role": d.get("role", "user"), "content": content})

    ctx = RuntimeExecution(
        session_id=session_id,
        run_id=run_id,
        cancellation=cancellation,
        max_steps=max_steps,
        budget_tokens=budget_tokens,
        conversation=ConversationSnapshot(messages=tuple(msg_dicts)),
    )

    from runtime_core.runtime import AgentRuntime
    runtime = AgentRuntime(ports)
    outcome = runtime.run(ctx)

    return outcome


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 5: Background execution
# ═══════════════════════════════════════════════════════════════════════════════

import threading as _threading_mod


class BackgroundChildHandle:
    """CC background subagent handle — returned immediately on async launch.

    agent_id: child session identifier (CC: agentId)
    """

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self._event = _threading_mod.Event()
        self._outcome: "RuntimeOutcome | None" = None

    @property
    def status(self) -> str:
        if self._outcome is None:
            return "running"
        return self._outcome.status.value

    @property
    def result(self) -> "RuntimeOutcome | None":
        """Non-blocking: None if still running, outcome if completed."""
        return self._outcome

    def wait(self, timeout: float | None = None) -> "RuntimeOutcome | None":
        """Block until child completes or timeout expires."""
        if self._event.wait(timeout):
            return self._outcome
        return None

    def _signal_complete(self, outcome: "RuntimeOutcome") -> None:
        """Internal: signal completion from the background thread."""
        self._outcome = outcome
        self._event.set()


def run_native_child_background(
    ports: RuntimePorts,
    session_id: SessionId,
    run_id: RunId,
    conversation: NativeConversation,
    cancellation: CancellationHandle,
    max_steps: int,
    budget_tokens: int,
    *,
    completion_callback: "Callable[[RuntimeOutcome], None] | None" = None,
) -> BackgroundChildHandle:
    """Execute one child run in a background daemon thread.

    CC-aligned: returns BackgroundChildHandle immediately (like CC's
    async_launched response).  Child runs to completion in a daemon thread.
    Call handle.wait() to block, or check handle.result non-blocking.
    """
    handle = BackgroundChildHandle(agent_id=str(session_id))

    def _run() -> None:
        try:
            outcome = run_native_child(
                ports=ports, session_id=session_id, run_id=run_id,
                conversation=conversation, cancellation=cancellation,
                max_steps=max_steps, budget_tokens=budget_tokens,
            )
        except Exception:
            from runtime_core.outcome import RuntimeOutcome, RunStatus
            outcome = RuntimeOutcome.failed(
                run_id, error="Background child execution failed",
            )
        handle._signal_complete(outcome)
        if completion_callback is not None:
            completion_callback(outcome)

    thread = _threading_mod.Thread(
        target=_run, daemon=True, name=f"child-{session_id}",
    )
    thread.start()

    return handle


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 6: Worktree isolation
# ═══════════════════════════════════════════════════════════════════════════════

def _check_worktree_changes(repo_path: str) -> bool:
    """Check if a worktree/workspace has uncommitted changes.

    Uses ``git status --porcelain`` — returns True if any tracked or
    untracked changes exist.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repo_path, capture_output=True, text=True, timeout=30,
        )
        return bool(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def run_native_child_in_worktree(
    repo_path: str,
    definition_name: str,
    agent_id: str,
    ports: RuntimePorts,
    conversation: NativeConversation,
    session_id: SessionId,
    run_id: RunId,
    max_steps: int,
    budget_tokens: int,
    cancellation: "CancellationHandle | None" = None,
) -> tuple[RuntimeOutcome, str]:
    """CC worktree isolation — one child in one isolated git worktree.

    1. WorktreeManager.create() → isolated worktree directory
    2. run_native_child() inside the worktree
    3. git status --porcelain → detect changes
    4. no changes → git worktree remove + branch -D  → disposition='discarded'
    5. has changes → preserve worktree                  → disposition='preserved'

    Uses ``agent.session.worktree_manager.WorktreeManager`` — a clean module
    with zero SessionRuntime / LLMMessage dependencies (Phase 10 safe).
    """
    from agent.session.worktree_manager import WorktreeManager

    if cancellation is None:
        cancellation = CancellationHandle()

    manager = WorktreeManager(repo_path)
    wt = manager.create(f"agent-{definition_name}-{agent_id}")

    try:
        outcome = run_native_child(
            ports=ports,
            session_id=session_id,
            run_id=run_id,
            conversation=conversation,
            cancellation=cancellation,
            max_steps=max_steps,
            budget_tokens=budget_tokens,
        )
        has_changes = _check_worktree_changes(wt.path)
        if has_changes:
            return outcome, "preserved"
        manager.discard(wt)
        return outcome, "discarded"
    except Exception:
        # Error path: always clean up the worktree
        try:
            manager.discard(wt)
        except Exception:
            pass
        raise
