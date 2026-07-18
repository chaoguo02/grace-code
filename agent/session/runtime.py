"""V2 Session Runtime — fresh-context child-session orchestration."""

from __future__ import annotations

import copy
import logging
import threading
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from agent.core import AgentConfig, ReActAgent
from agent.event_log import EventLog
from agent.task import Event, EventType, RunResult, RunStatus, Task, TaskIntent
from agent.session.agent_registry import AgentRegistryV2
from agent.session.models import (
    AgentCompletionNotification,
    AgentCancelOutcome,
    AgentCancelResult,
    AgentDefinition,
    AgentKind,
    AgentSpawnRequest,
    ContextOrigin,
    DelegationOrigin,
    DelegationScope,
    ExplicitDelegationRequest,
    ExecutionPlacement,
    AgentRunResult,
    AgentRunStatus,
    AgentMessageOutcome,
    AgentMessageReceipt,
    AgentWaitOutcome,
    AgentWaitResult,
    BackgroundAgentHandle,
    ForkStatus,
    ManagedWorktreeRecord,
    SessionMode,
    SessionStatus,
    WorktreeChange,
    WorktreeAvailability,
    WorktreeDisposition,
    WorktreeEvidence,
    WorkspaceMode,
)
from agent.session.session_store import SessionStore
from agent.session.subagent import run_child_agent
from agent.session.run_context import (
    AgentSpawnContext, CancellationToken, ToolSchemaSnapshot,
)
from context.history import ConversationHistory
from hooks.events import HookContext, HookEvent, SessionStartSource
from hooks.protocol import DispatchResult
from llm.base import LLMBackend, LLMMessage
from core.base import ToolRegistry

logger = logging.getLogger(__name__)


def _resolve_permission_pipeline(agent: "ReActAgent") -> "PermissionPipeline | None":
    """Walk the registry chain to find the PermissionPipeline.

    Chain: ReActAgent._full_registry (PolicyAwareToolRegistry)
           → ._base (ToolRegistry)
           → ._permission_pipeline (PermissionPipeline)
    """
    registry = getattr(agent, "_full_registry", None)
    if registry is None:
        return None
    # PolicyAwareToolRegistry wraps a ToolRegistry at ._base
    base = getattr(registry, "_base", None)
    if base is not None:
        return getattr(base, "_permission_pipeline", None)
    # Fallback: check the wrapped registry directly
    return getattr(registry, "_permission_pipeline", None)


class ExplicitDelegationError(ValueError):
    """An explicit child request cannot be honored by the parent contract."""

if TYPE_CHECKING:
    from agent.completion_guard import CompletionCheckResult
    from core.policy import PhasePolicy
    from agent.session.models import SessionRecord
    from agent.session.worktree_service import WorktreeOperationResult
    from agent.session.worktree_manager import Worktree


class SessionRuntime:
    """V2 session runtime with fresh-context subagent orchestration.

    Coordinator agents (build, plan) carry the `task` tool and can
    dispatch child subagents. Each child runs in a fresh context with
    tools restricted to its agent definition allow-list.
    """

    def __init__(
        self,
        *,
        store: SessionStore,
        backend: LLMBackend,
        base_registry: ToolRegistry,
        agent_registry: AgentRegistryV2,
        root_agent_config: AgentConfig,
        log_dir: str,
        memory_context=None,
        hook_dispatcher=None,
        mcp_integration=None,
        event_callback=None,
    ) -> None:
        self._store = store
        self._backend = backend
        self._base_registry = base_registry
        self._agent_registry = agent_registry
        self._root_agent_config = root_agent_config
        self._log_dir = log_dir
        self._memory_context = memory_context
        self._hook_dispatcher = hook_dispatcher
        self._mcp_integration = mcp_integration
        self._event_callback = event_callback
        # Per-session ApprovalBroker instances for headless Web mode.
        # CC-aligned: each session has its own blocking approval queue,
        # equivalent to CC's per-session stdin control_request channel.
        self._approval_brokers: dict[str, "ApprovalBroker"] = {}
        # Per-session web_confirm_callback factories, set by agent_service
        # before run_session().  keyed by session_id.
        self._web_confirm_callbacks: dict[str, "WebConfirmCallback"] = {}
        self._cancellation_tokens: dict[tuple[str, int], CancellationToken] = {}
        self._background_runs: dict[tuple[str, int], threading.Thread] = {}
        self._background_runs_lock = threading.Lock()

        # ── Circuit Breaker (code-level, not prompt-based) ──
        from core.circuit_breaker import CircuitBreaker
        self._circuit_breaker = CircuitBreaker()

        # ── P1-6: Dynamic Capability Registry ──
        from agent.capability_registry import CapabilityRegistry
        self._capability_registry = CapabilityRegistry()
        # Register all builtin tools from the base registry
        self._capability_registry.register_bulk(self._base_registry.tool_names)
        # Wire the registry into the base ToolRegistry for physical interception
        self._base_registry._capability_registry = self._capability_registry
        # Mark MCP tools as UNAVAILABLE if the bridge failed to connect
        self._sync_mcp_capabilities()

    @property
    def agent_registry(self) -> AgentRegistryV2:
        return self._agent_registry

    @property
    def circuit_breaker(self):
        return self._circuit_breaker

    @property
    def capability_registry(self):
        return self._capability_registry

    def cancel_session(self, session_id: str, detail: str = "") -> bool:
        """Cancel one active session; hierarchical tokens propagate to descendants."""
        session = self._store.get_session(session_id)
        if session is None:
            return False
        token = self._cancellation_tokens.get((session_id, session.generation))
        if token is None:
            return False
        token.cancel(detail=detail)
        return True

    def _require_project_scope(self, repo_path: str) -> str:
        """Normalize and verify a repo against this Runtime's registry scope."""
        normalized = str(Path(repo_path).expanduser().resolve())
        if self._agent_registry.project_dir != normalized:
            raise ValueError(
                "Agent registry project scope does not match the execution repo: "
                f"registry={self._agent_registry.project_dir!r}, repo={normalized!r}"
            )
        return normalized

    def get_session_repo_path(self, session_id: str) -> str:
        """Return a verified parent-session project root or fail closed."""
        session = self._store.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown v2 session: {session_id}")
        return self._require_project_scope(session.repo_path)

    def inspect_subagent_worktree(
        self, parent_session_id: str, child_session_id: str,
    ) -> WorktreeEvidence:
        """Return fresh Git facts for one direct child's available worktree."""
        _, _, worktree = self._require_available_worktree(
            parent_session_id, child_session_id,
        )
        from agent.session.worktree_service import inspect_worktree
        return inspect_worktree(worktree)

    def list_managed_worktrees(self) -> list[ManagedWorktreeRecord]:
        """Join persisted retained/preserved sessions with fresh Git facts."""
        records: list[ManagedWorktreeRecord] = []
        sessions = self._store.list_worktree_sessions(frozenset({
            WorktreeDisposition.PRESERVED,
            WorktreeDisposition.RETAINED,
        }))
        for child in sessions:
            result = child.agent_result
            if (
                result is None
                or result.worktree is None
                or child.parent_id is None
            ):
                continue
            try:
                evidence = self.inspect_subagent_worktree(
                    child.parent_id, child.id,
                )
                availability = WorktreeAvailability.AVAILABLE
                error = ""
            except ValueError as exc:
                evidence = result.worktree
                availability = WorktreeAvailability.UNAVAILABLE
                error = str(exc)
            records.append(ManagedWorktreeRecord(
                child_session_id=child.id,
                parent_session_id=child.parent_id,
                disposition=result.worktree_disposition,
                availability=availability,
                evidence=evidence,
                error=error,
            ))
        return records

    def apply_subagent_worktree(
        self,
        parent_session_id: str,
        child_session_id: str,
        *,
        expected_revision: str,
    ) -> "WorktreeOperationResult":
        """Explicitly apply one reviewed child result to the current branch."""
        child, fork_result, worktree = self._require_available_worktree(
            parent_session_id, child_session_id,
        )
        from agent.session.worktree_service import (
            WorktreeOperationStatus,
            apply_worktree,
        )
        result = apply_worktree(
            worktree,
            child.repo_path,
            expected_revision=expected_revision,
        )
        if result.status in {
            WorktreeOperationStatus.APPLIED,
            WorktreeOperationStatus.NO_CHANGES,
        }:
            disposition = (
                WorktreeDisposition.APPLIED
                if result.status is WorktreeOperationStatus.APPLIED
                else WorktreeDisposition.CLEANED
            )
            self._store.set_agent_result(
                child.id,
                replace(
                    fork_result,
                    worktree=None,
                    worktree_disposition=disposition,
                ),
            )
        return result

    def discard_subagent_worktree(
        self,
        parent_session_id: str,
        child_session_id: str,
        *,
        expected_revision: str,
    ) -> "WorktreeOperationResult":
        """Explicitly discard one reviewed child result."""
        child, fork_result, worktree = self._require_available_worktree(
            parent_session_id, child_session_id,
        )
        from agent.session.worktree_service import (
            WorktreeOperationStatus,
            discard_reviewed_worktree,
        )
        result = discard_reviewed_worktree(
            worktree,
            child.repo_path,
            expected_revision=expected_revision,
        )
        if result.status is WorktreeOperationStatus.DISCARDED:
            self._store.set_agent_result(
                child.id,
                replace(
                    fork_result,
                    worktree=None,
                    worktree_disposition=WorktreeDisposition.DISCARDED,
                ),
            )
        return result

    def retain_subagent_worktree(
        self,
        parent_session_id: str,
        child_session_id: str,
        *,
        expected_revision: str,
    ) -> "WorktreeOperationResult":
        """Explicitly retain an unapplied child worktree for later handling."""
        child, fork_result, worktree = self._require_available_worktree(
            parent_session_id, child_session_id,
        )
        from agent.session.worktree_service import (
            WorktreeOperationResult,
            WorktreeOperationStatus,
            inspect_worktree,
        )
        evidence = inspect_worktree(worktree)
        if evidence.change is WorktreeChange.UNKNOWN:
            return WorktreeOperationResult(
                WorktreeOperationStatus.FAILED,
                evidence,
                evidence.error or "Unable to inspect child worktree",
            )
        if evidence.revision != expected_revision:
            return WorktreeOperationResult(
                WorktreeOperationStatus.STALE,
                evidence,
                "Child worktree revision changed after review",
            )
        self._store.set_agent_result(
            child.id,
            replace(
                fork_result,
                worktree=evidence,
                worktree_disposition=WorktreeDisposition.RETAINED,
            ),
        )
        return WorktreeOperationResult(
            WorktreeOperationStatus.RETAINED, evidence,
        )

    def _require_available_worktree(
        self, parent_session_id: str, child_session_id: str,
    ) -> tuple["SessionRecord", AgentRunResult, "Worktree"]:
        """Resolve a persisted worktree handle without trusting stored paths."""
        parent = self._store.get_session(parent_session_id)
        if parent is None:
            raise ValueError(f"Unknown parent session: {parent_session_id}")
        self._require_project_scope(parent.repo_path)
        child = self._store.get_session(child_session_id)
        if child is None or child.parent_id != parent.id:
            raise ValueError("Worktree session must be a direct child of the caller")
        if child.repo_path != parent.repo_path:
            raise ValueError("Parent and child project roots do not match")
        fork_result = child.agent_result
        if (
            fork_result is None
            or fork_result.worktree_disposition not in {
                WorktreeDisposition.PRESERVED,
                WorktreeDisposition.RETAINED,
            }
            or fork_result.worktree is None
        ):
            raise ValueError("Child session has no available worktree result")

        evidence = fork_result.worktree
        from core.state_paths import ProjectStatePaths
        allowed_root = ProjectStatePaths.for_project(parent.repo_path).worktrees.resolve()
        worktree_path = Path(evidence.path).resolve()
        try:
            worktree_path.relative_to(allowed_root)
        except ValueError as exc:
            raise ValueError("Stored child worktree path is outside Agent state") from exc

        from core.process import LocalRuntime
        parent_runtime = LocalRuntime(workspace_root=parent.repo_path)
        listed = parent_runtime.execute(
            "git", args=["worktree", "list", "--porcelain"],
            cwd=parent.repo_path, timeout=30,
        )
        if not listed.success:
            raise ValueError(listed.stderr or "Unable to list project worktrees")
        registered: dict[Path, str] = {}
        listed_path: Path | None = None
        for line in listed.stdout.splitlines():
            if line.startswith("worktree "):
                listed_path = Path(line.removeprefix("worktree ")).resolve()
                registered.setdefault(listed_path, "")
            elif line.startswith("branch ") and listed_path is not None:
                registered[listed_path] = line.removeprefix("branch refs/heads/")
        if worktree_path not in registered:
            raise ValueError("Stored child worktree is not registered with the project")
        if registered[worktree_path] != evidence.branch:
            raise ValueError("Stored child worktree branch does not match Git facts")

        from agent.session.worktree_manager import Worktree
        worktree = Worktree(
            name=worktree_path.name,
            path=str(worktree_path),
            branch=evidence.branch,
            base_branch=evidence.base_branch,
            base_commit=evidence.base_commit,
        )
        return child, fork_result, worktree

    def _check_session_completion(
        self, session_id: str,
    ) -> "CompletionCheckResult":
        """Block success while direct-child worktrees await an explicit decision."""
        from agent.completion_guard import CompletionCheckResult

        pending = []
        for child in self._store.list_child_sessions(session_id):
            result = child.agent_result
            if (
                result is not None
                and result.worktree_disposition is WorktreeDisposition.PRESERVED
                and result.worktree is not None
            ):
                pending.append((child.id, result.worktree))
        if not pending:
            return CompletionCheckResult(can_complete=True)

        facts = "\n".join(
            f"- child_session_id={child_id}; path={evidence.path}; "
            f"revision={evidence.revision}"
            for child_id, evidence in pending
        )
        return CompletionCheckResult(
            can_complete=False,
            blocked_reason="Unresolved preserved subagent worktree",
            inject_message=(
                "[RUNTIME BLOCK] One or more child worktrees are still preserved. "
                "Their changes are not present in the parent workspace. Inspect each "
                "child, then explicitly apply, discard, or retain it before finishing.\n"
                f"{facts}"
            ),
        )

    # ── Root session ──

    def create_root_session(
        self,
        *,
        agent_name: str,
        repo_path: str,
        title: str,
        metadata: dict | None = None,
    ):
        spec = self._agent_registry.get(agent_name)
        if spec.agent_kind is not AgentKind.PRIMARY:
            raise ValueError(
                f"Agent {agent_name!r} is not declared as a primary entrypoint"
            )
        normalized_repo = self._require_project_scope(repo_path)
        return self._store.create_session(
            agent_name=agent_name,
            mode=SessionMode.PRIMARY,
            agent_kind=AgentKind.PRIMARY,
            context_origin=ContextOrigin.FRESH,
            execution_placement=ExecutionPlacement.FOREGROUND,
            workspace_mode=WorkspaceMode.CURRENT,
            repo_path=normalized_repo,
            title=title,
            metadata=metadata or {},
        )

    def run_explicit_delegation(
        self,
        parent_session_id: str,
        *,
        request: ExplicitDelegationRequest,
        parent_intent: TaskIntent,
        contract: "TaskContract",
    ) -> AgentRunResult:
        """Guarantee one named child run without asking the parent model to route it."""
        from core.policy import PhasePolicy, READ_ONLY_EFFECTS
        from agent.session.task_contract import TaskContract
        from core.base import ToolEffect, ToolRole

        if not isinstance(request, ExplicitDelegationRequest):
            raise TypeError("request must be an ExplicitDelegationRequest")
        if not isinstance(parent_intent, TaskIntent):
            parent_intent = TaskIntent(parent_intent)
        if not isinstance(contract, TaskContract):
            raise TypeError("contract must be a TaskContract")

        parent = self._store.get_session(parent_session_id)
        if parent is None:
            raise ExplicitDelegationError(
                f"Unknown parent session: {parent_session_id}"
            )
        if parent.mode is not SessionMode.PRIMARY:
            raise ExplicitDelegationError(
                "Explicit delegation requires a primary parent session"
            )
        parent_definition = self._agent_registry.get(parent.agent_name)
        allowed = {
            child.name: child
            for child in self._agent_registry.delegatable_by(parent_definition)
        }
        definition = allowed.get(request.agent_name)
        if definition is None:
            raise ExplicitDelegationError(
                f"Agent {request.agent_name!r} is not delegatable by "
                f"{parent.agent_name!r}. Available: {sorted(allowed)}"
            )
        if (
            parent_intent is TaskIntent.ANALYSIS
            and definition.intent is not TaskIntent.ANALYSIS
        ):
            raise ExplicitDelegationError(
                f"Analysis task cannot explicitly delegate to write-capable "
                f"agent {request.agent_name!r}"
            )

        # Derive authority from tools physically visible to this parent rather
        # than from the requested child name or task prose.
        parent_registry = self._build_registry_for_session(parent_definition, parent)
        allowed_effects = {ToolEffect.PRODUCE_DELIVERABLE}
        for tool_name in parent_registry.tool_names:
            metadata = parent_registry.metadata_for(tool_name)
            if metadata is not None and ToolRole.DELEGATE not in metadata.roles:
                allowed_effects.update(metadata.effects)
        if (
            parent_intent is TaskIntent.ANALYSIS
            or parent_definition.effective_delegation_scope
            is DelegationScope.READ_ONLY
        ):
            allowed_effects.intersection_update(READ_ONLY_EFFECTS)

        return self.fork_session(
            parent_session_id=parent.id,
            definition=definition,
            description=request.description,
            prompt=request.prompt,
            budget_tokens=contract.budget_tokens,
            parent_max_steps=contract.max_steps,
            cancellation_token=CancellationToken(),
            parent_policy=PhasePolicy(
                allowed_effects=frozenset(allowed_effects)
            ),
            origin=DelegationOrigin.EXPLICIT,
        )

    def finalize_parent_from_explicit_child(
        self, parent_session_id: str, child_result: AgentRunResult,
    ) -> None:
        """Converge an unrun parent when explicit delegation is terminal."""
        parent = self._store.get_session(parent_session_id)
        if parent is None:
            raise ExplicitDelegationError(
                f"Unknown parent session: {parent_session_id}"
            )
        status = child_result.status.session_status
        if status in {SessionStatus.FAILED, SessionStatus.CANCELLED}:
            self._store.update_status(
                parent.id,
                status,
                error=child_result.error or child_result.summary,
            )
        self._store.set_summary(parent.id, child_result.summary, status=status)

    def run_session(
        self,
        session_id: str,
        *,
        agent_name: str,
        task_description: str,
        intent: TaskIntent | str | None = None,
        messages: list[LLMMessage] | None = None,
        max_steps_override: int | None = None,        # deprecated: use contract
        budget_tokens_override: int | None = None,    # deprecated: use contract
        contract: "TaskContract | None" = None,
        inject_rules: list | None = None,              # Web: permission rules from settings
        inject_permission_mode: str | None = None,     # Web: "acceptEdits" / "default" / etc.
    ) -> RunResult:
        session = self._store.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown v2 session: {session_id}")
        self._require_project_scope(session.repo_path)

        # The selected agent is an explicit entrypoint decision. Runtime does
        # not override it by interpreting task prose.
        _effective_agent = agent_name

        # ── Phase 7: State finalization gate — status update is NOT a step in the flow,
        #     it's an inevitable consequence. try/finally ensures convergence. ──
        self._store.update_status(session_id, SessionStatus.RUNNING)
        result: RunResult | None = None
        cancellation_token = CancellationToken()
        session_key = (session_id, session.generation)
        self._cancellation_tokens[session_key] = cancellation_token
        execution_error: BaseException | None = None

        try:
            # ── Session memory tracker ──
            session_memory_tracker = None
            if self._root_agent_config is not None and self._root_agent_config.session_notes:
                from memory.session_memory import SessionMemoryTracker
                _notes_dir = Path(session.repo_path) / ".grace" / "v2" / "sessions" / session_id
                _notes_dir.mkdir(parents=True, exist_ok=True)
                _notes_path = _notes_dir / "session_notes.md"
                session_memory_tracker = SessionMemoryTracker(
                    backend=self._backend,
                    notes_path=_notes_path,
                    session_title=f"Session {session_id[:8]}",
                )

            from agent.session.agent_factory import AgentFactory
            _assembly = AgentFactory.create(
                agent_name=_effective_agent,
                backend=self._backend,
                base_registry=self._base_registry,
                agent_registry=self._agent_registry,
                root_agent_config=self._root_agent_config,
                memory_context=self._memory_context,
                session=session,
                circuit_breaker=self._circuit_breaker,
                runtime=self,
                mcp_tool_names=self._mcp_tool_names_for_spec(
                    self._agent_registry.get(_effective_agent)
                ),
                session_memory_tracker=session_memory_tracker,
            )
            spec = _assembly.spec
            effective_intent = TaskIntent(intent) if intent is not None else spec.intent
            _eff_contract = contract if contract is not None else _assembly.contract
            agent = _assembly.agent
            agent_cfg = _assembly.agent_cfg

            # ── Inject web_confirm_callback into the PermissionPipeline ──
            # CC-aligned: in headless Web mode, the pipeline's Layer 6
            # blocks on threading.Event instead of stdin.  The callback,
            # rules, and permission mode are passed as explicit parameters
            # (rather than shared instance attributes) so concurrent
            # sessions cannot interfere.
            _pipeline = _resolve_permission_pipeline(agent)
            if _pipeline is not None:
                # Only pop the callback when we can actually use it
                _web_cb = self._web_confirm_callbacks.pop(session_id, None)
                if _web_cb is not None:
                    _pipeline._web_confirm_callback = _web_cb
                # Inject loaded permission rules (from settings.json)
                if inject_rules:
                    for rule in inject_rules:
                        if rule.tier.value == "deny":
                            _pipeline._deny_rules.append(rule)
                        elif rule.tier.value == "ask":
                            _pipeline._ask_rules.append(rule)
                        elif rule.tier.value == "allow":
                            _pipeline._allow_rules.append(rule)
                # Set permission mode
                if inject_permission_mode:
                    _pipeline.set_permission_mode(inject_permission_mode)

            agent_cfg.cancellation_token = cancellation_token
            agent_cfg.completion_fact_check = (
                lambda: self._check_session_completion(session_id)
            )
            # CC-aligned plan mode throttling: full injection on turn 1,
            # sparse reminder every 5 turns, full re-injection every 25 turns.
            _base_msg_source = lambda: (
                self._claim_completion_messages(session_id)
                + self._claim_new_messages(session_id)
            )
            if spec.permission_mode == "plan":
                _plan_step = [0]
                def _plan_throttled_source():
                    _plan_step[0] += 1
                    _step = _plan_step[0]
                    _msgs = list(_base_msg_source())
                    if _step == 1:
                        return _msgs  # full injection already in build_runtime_messages
                    if _step % 5 == 0 and _step % 25 != 0:
                        _msgs.append(LLMMessage(role="user", content=(
                            "[PLAN MODE] You are still in plan mode. "
                            "Analysis only — no edits. Produce a structured "
                            "plan with a JSON contract before finishing."
                        )))
                    elif _step % 25 == 0:
                        from prompts.builder import get_plan_mode_injection
                        _msgs.append(LLMMessage(
                            role="user", content=get_plan_mode_injection(),
                        ))
                    return _msgs
                agent_cfg.runtime_message_source = _plan_throttled_source
            else:
                agent_cfg.runtime_message_source = _base_msg_source
            agent_cfg.stop_hook_event = HookEvent.STOP
            agent_cfg.hook_session_id = session_id
            agent_cfg.hook_agent_id = ""
            agent_cfg.hook_agent_type = spec.name
            agent_cfg.hook_dispatcher = self._hook_dispatcher

            persisted_messages = self._store.list_messages(session_id)
            had_persisted_messages = bool(persisted_messages)
            if messages:
                for message in messages:
                    self._store.append_message(session_id, message)
                persisted_messages = self._store.list_messages(session_id)
            elif not persisted_messages:
                self._store.append_message(session_id, LLMMessage(role="user", content=task_description))
                persisted_messages = self._store.list_messages(session_id)

            history = ConversationHistory(max_messages=agent_cfg.history_max_messages)
            injected_messages = self._build_runtime_messages(spec, task_description)
            history.add_many(injected_messages + persisted_messages)
            agent._pending_history = history

            task = Task(
                description=task_description,
                repo_path=session.repo_path,
                intent=effective_intent,
                max_steps=(max_steps_override or _eff_contract.max_steps if _eff_contract else agent_cfg.max_steps),
                budget_tokens=(budget_tokens_override or _eff_contract.budget_tokens if _eff_contract else agent_cfg.budget_tokens),
                metadata={
                    "entrypoint": "v2",
                    "mode": f"v2-{agent_name}",
                    "session_id": session_id,
                    "parent_session_id": session.parent_id,
                    "root_session_id": session.root_id,
                    "agent_name": agent_name,
                    "agent_depth": session.agent_depth.value,
                    "v2_bypass_path_scope_policy": True,
                    "v2_disable_legacy_analysis_prompting": True,
                    "completion_requires": dict(spec.completion_requires),
                    "required_tools": sorted(spec.required_tools),
                },
            )

            start_source = (
                SessionStartSource.STARTUP
                if session.status is SessionStatus.QUEUED and not had_persisted_messages
                else SessionStartSource.RESUME
            )
            start_hook = self._fire_hook(HookContext(
                event=HookEvent.SESSION_START,
                session_id=session_id,
                agent_type=spec.name,
                session_start_source=start_source,
            ))
            if start_hook.additional_context:
                history.add(LLMMessage(
                    role="user",
                    content=(
                        "[SESSION START HOOK CONTEXT]\n"
                        f"{start_hook.additional_context}"
                    ),
                ))

            # Runtime-injected messages are also in history. Counting only DB
            # messages re-appends old history and can split native tool pairs.
            initial_count = len(history)
            with EventLog.create(task, log_dir=self._log_dir) as log:
                if self._event_callback is not None:
                    original_append = log._append
                    _captured_session_id = session_id

                    def _append_and_emit(event):
                        event.session_id = _captured_session_id
                        original_append(event)
                        try:
                            self._event_callback(event)
                        except Exception:
                            logger.debug("V2 event callback failed", exc_info=True)

                    log._append = _append_and_emit
                result = agent.run(task, log)

            for message in history.to_list()[initial_count:]:
                self._store.append_message(session_id, message)

            return result
        except KeyboardInterrupt as exc:
            execution_error = exc
            cancellation_token.cancel(detail="user interrupted session execution")
            raise
        except BaseException as exc:
            execution_error = exc
            raise
        finally:
            # ── Phase 7: State convergence — ALWAYS runs, regardless of path ──
            if result is not None:
                if result.status is RunStatus.CANCELLED:
                    self._store.update_status(
                        session_id, SessionStatus.CANCELLED,
                        error=result.error or result.summary,
                    )
                    self._store.set_summary(
                        session_id, result.summary, status=SessionStatus.CANCELLED,
                    )
                elif result.is_success():
                    self._store.set_summary(
                        session_id, result.summary, status=SessionStatus.COMPLETED
                    )
                else:
                    self._store.update_status(
                        session_id,
                        SessionStatus.FAILED,
                        error=result.error or result.summary,
                    )
                    self._store.set_summary(
                        session_id, result.summary, status=SessionStatus.FAILED
                    )
            elif cancellation_token.is_cancelled:
                detail = cancellation_token.detail
                self._store.update_status(
                    session_id, SessionStatus.CANCELLED, error=detail,
                )
                self._store.set_summary(
                    session_id, f"Task cancelled: {detail}",
                    status=SessionStatus.CANCELLED,
                )
            elif execution_error is not None:
                detail = str(execution_error) or type(execution_error).__name__
                self._store.update_status(
                    session_id, SessionStatus.FAILED, error=detail,
                )
                self._store.set_summary(
                    session_id, "Session execution failed before producing a result",
                    status=SessionStatus.FAILED,
                )
            self._cancellation_tokens.pop(session_key, None)

    # ── Child subagent ──

    def spawn_agent(
        self,
        *,
        parent_session_id: str,
        request: AgentSpawnRequest,
        budget_tokens: int,
        parent_max_steps: int,
        cancellation_token: CancellationToken,
        parent_policy: "PhasePolicy",
        origin: DelegationOrigin = DelegationOrigin.TOOL,
        spawn_context: AgentSpawnContext | None = None,
    ) -> AgentRunResult | BackgroundAgentHandle:
        """Create and run one typed child through the unified spawn path.

        Named children use their definition and a fresh context. Forks use the
        parent's immutable model-input snapshot and reconstructed tool contract.
        """
        if budget_tokens <= 0:
            raise ValueError("child budget_tokens must be positive")
        if parent_max_steps <= 0:
            raise ValueError("child parent_max_steps must be positive")
        if not isinstance(cancellation_token, CancellationToken):
            raise TypeError("child cancellation_token must be a CancellationToken")
        from core.policy import PhasePolicy
        if not isinstance(parent_policy, PhasePolicy):
            raise TypeError("child parent_policy must be a PhasePolicy")
        if not isinstance(request, AgentSpawnRequest):
            raise TypeError("request must be an AgentSpawnRequest")
        if not isinstance(origin, DelegationOrigin):
            origin = DelegationOrigin(origin)
        parent = self._store.get_session(parent_session_id)
        if parent is None:
            raise ValueError(f"Unknown v2 session: {parent_session_id}")
        if not parent.agent_depth.can_spawn:
            raise ValueError("Maximum subagent depth reached")
        parent_definition = self._agent_registry.get(parent.agent_name)
        if request.agent_kind is AgentKind.NAMED_SUBAGENT:
            definition = request.definition
            if definition is None:
                raise ValueError("Named spawn requires a definition")
            allowed_names = {
                child.name
                for child in self._agent_registry.delegatable_by(parent_definition)
            }
            if definition.name not in allowed_names:
                raise ValueError(
                    f"Agent {definition.name!r} is not delegatable by "
                    f"{parent.agent_name!r}"
                )
        else:
            if parent.agent_kind is AgentKind.FORK:
                raise ValueError("A fork cannot spawn another fork")
            if spawn_context is None:
                raise ValueError("Fork spawn requires a live parent snapshot")
            definition = parent_definition
        is_fork = request.agent_kind is AgentKind.FORK
        from agent.session.task_contract import TaskContract
        child_contract = TaskContract.for_subagent(
            definition,
            self._root_agent_config,
            parent_budget_tokens=budget_tokens,
            parent_max_steps=parent_max_steps,
        )
        _repo = self._require_project_scope(parent.repo_path)
        if spawn_context is not None:
            if not isinstance(spawn_context, AgentSpawnContext):
                raise TypeError("spawn_context must be an AgentSpawnContext")
            if spawn_context.parent_session_id != parent.id:
                raise ValueError("spawn context parent does not match the session")
            if spawn_context.parent_agent_name != parent.agent_name:
                raise ValueError("spawn context agent does not match the session")
            if self._require_project_scope(spawn_context.repo_path) != _repo:
                raise ValueError("spawn context repo does not match the session")
            if (
                request.agent_kind is AgentKind.FORK
                and spawn_context.model_name != self._backend.model_name
            ):
                raise ValueError("Fork model must match the parent model")
        child_agent_type = (
            AgentKind.FORK.value
            if request.agent_kind is AgentKind.FORK
            else definition.name
        )
        child = self._store.create_session(
            agent_name=definition.name,
            mode=SessionMode.SUBAGENT,
            agent_kind=request.agent_kind,
            context_origin=request.context_origin,
            execution_placement=request.execution_placement,
            workspace_mode=request.workspace_mode,
            repo_path=_repo,
            title=request.description[:80] or definition.name,
            parent_id=parent.id,
            root_id=parent.root_id,
            metadata={
                "entrypoint": origin.value,
                "agent_kind": request.agent_kind.value,
                "context_origin": request.context_origin.value,
                "workspace_mode": request.workspace_mode.value,
                "intent": definition.intent.value,
                "requested_budget_tokens": budget_tokens,
                "budget_tokens": child_contract.budget_tokens,
                "max_steps": child_contract.max_steps,
                "parent_policy": parent_policy.to_dict(),
                "parent_snapshot_fingerprint": (
                    spawn_context.conversation.fingerprint
                    if spawn_context is not None else None
                ),
                "parent_snapshot_message_count": (
                    len(spawn_context.conversation.messages)
                    if spawn_context is not None else 0
                ),
                "model_name": (
                    spawn_context.model_name
                    if spawn_context is not None else self._backend.model_name
                ),
                "parent_tool_schemas": (
                    [
                        {
                            "name": schema.name,
                            "description": schema.description,
                            "parameters_json": schema.parameters_json,
                        }
                        for schema in spawn_context.tool_schemas
                    ]
                    if request.agent_kind is AgentKind.FORK
                    and spawn_context is not None
                    else []
                ),
            },
        )
        child_cancellation = cancellation_token.child()
        self._cancellation_tokens[(child.id, child.generation)] = child_cancellation
        if request.agent_kind is AgentKind.FORK:
            for message in spawn_context.conversation.materialize():
                self._store.append_message(child.id, message)
        self._store.append_message(
            child.id, LLMMessage(role="user", content=request.prompt)
        )
        self._store.update_status(child.id, SessionStatus.RUNNING)
        self._emit_subagent_event(
            EventType.SUBAGENT_START,
            parent_session_id=parent.id,
            root_session_id=parent.root_id,
            child_session_id=child.id,
            agent_name=child_agent_type,
            status=SessionStatus.RUNNING,
        )
        self._fire_hook(HookContext(
            event=HookEvent.SUBAGENT_START,
            session_id=parent.id,
            agent_id=child.id,
            agent_type=child_agent_type,
        ))

        # Subagent permission inheritance (CC-aligned: parent mode overrides child)
        # Store resolved mode in child metadata; _build_registry_for_session()
        # reads it to create a per-session pipeline without touching the shared one.
        _child_permission_mode = self._resolve_child_permission_mode(
            parent_definition, definition if request.agent_kind is AgentKind.NAMED_SUBAGENT else None
        )
        if _child_permission_mode:
            child.metadata["permission_mode_override"] = _child_permission_mode

        # Connect agent-scoped MCP servers (CC-aligned: inline mcpServers)
        _agent_mcp_tools = []
        if self._mcp_integration is not None and not is_fork:
            _agent_mcp_tools = self._mcp_integration.connect_agent_servers(definition)

        execute = lambda: self._execute_child_session(
            parent=parent,
            child=child,
            request=request,
            definition=definition,
            parent_definition=parent_definition,
            contract=child_contract,
            cancellation_token=child_cancellation,
            parent_policy=parent_policy,
            repo_path=_repo,
            child_agent_type=child_agent_type,
            spawn_context=spawn_context,
        )
        _need_mcp_cleanup = _agent_mcp_tools and self._mcp_integration is not None
        cleanup = None
        if _need_mcp_cleanup:
            cleanup = lambda: self._mcp_integration.disconnect_agent_servers(definition)

        if request.execution_placement is ExecutionPlacement.FOREGROUND:
            try:
                return execute()
            finally:
                if cleanup is not None:
                    cleanup()
        return self._start_background_execution(
            parent=parent,
            child=child,
            agent_name=definition.name,
            execute=execute,
            cleanup=cleanup,
        )

    def _execute_child_session(
        self,
        *,
        parent: "SessionRecord",
        child: "SessionRecord",
        request: AgentSpawnRequest,
        definition: AgentDefinition,
        parent_definition: AgentDefinition,
        contract: "TaskContract",
        cancellation_token: CancellationToken,
        parent_policy: "PhasePolicy",
        repo_path: str,
        child_agent_type: str,
        spawn_context: AgentSpawnContext | None,
        persisted_messages: list[LLMMessage] | None = None,
    ) -> AgentRunResult:
        """Execute one child generation and converge its persisted state."""
        child_result: AgentRunResult | None = None
        child_error = ""

        def _persist_child_messages(messages: list[LLMMessage]) -> None:
            for message in messages:
                self._store.append_message(child.id, message)

        try:
            inherited_registry = None
            if request.agent_kind is AgentKind.FORK:
                inherited_registry = self._build_registry_for_session(
                    parent_definition, child,
                ).with_phase_policy(parent_policy)
                if request.context_origin is ContextOrigin.PARENT_SNAPSHOT:
                    if spawn_context is None:
                        raise ValueError("Fork spawn requires a live parent snapshot")
                    live_schemas = tuple(
                        ToolSchemaSnapshot.capture(schema)
                        for schema in inherited_registry.get_schemas()
                    )
                    if live_schemas != spawn_context.tool_schemas:
                        raise ValueError(
                            "Fork tool contract changed after the parent model call"
                        )
                else:
                    raw_schemas = child.metadata.get("parent_tool_schemas")
                    if not isinstance(raw_schemas, list) or not raw_schemas:
                        raise ValueError(
                            "Fork resume requires its persisted tool contract"
                        )
                    expected_schemas = tuple(
                        ToolSchemaSnapshot(
                            name=str(item["name"]),
                            description=str(item["description"]),
                            parameters_json=str(item["parameters_json"]),
                        )
                        for item in raw_schemas
                        if isinstance(item, dict)
                    )
                    live_schemas = tuple(
                        ToolSchemaSnapshot.capture(schema)
                        for schema in inherited_registry.get_schemas()
                    )
                    if live_schemas != expected_schemas:
                        raise ValueError(
                            "Fork tool contract changed since its prior generation"
                        )
            # ── Snapshot parent pipeline state for child inheritance ──
            # CC-aligned: subagents inherit parent's deny/allow rules,
            # session_rules, and permission_mode (subject to constraints).
            _parent_pipeline = getattr(self._base_registry, '_permission_pipeline', None)
            _inherited_state = _parent_pipeline.get_inheritable_state() if _parent_pipeline else {}

            child_result = run_child_agent(
                agent_id=child.id,
                request=request,
                source_definition=definition,
                repo_path=repo_path,
                base_registry=self._base_registry,
                backend=self._backend,
                log_dir=self._log_dir,
                root_agent_config=self._root_agent_config,
                message_sink=_persist_child_messages,
                contract=contract,
                cancellation_token=cancellation_token,
                parent_policy=parent_policy,
                spawn_context=spawn_context,
                inherited_registry=inherited_registry,
                event_callback=self._event_callback,
                persisted_messages=persisted_messages,
                session_record=child,
                session_runtime=self,
                parent_pipeline_state=_inherited_state,
            )
            self._store.set_agent_result(child.id, child_result)
            self._store.append_message(
                child.id,
                LLMMessage(role="assistant", content=child_result.summary),
            )
            return child_result
        except Exception as exc:
            child_error = str(exc) or type(exc).__name__
            self._store.append_message(
                child.id,
                LLMMessage(role="assistant", content=f"Subagent failed: {exc}"),
            )
            raise
        finally:
            if child_result is not None and child_result.status is ForkStatus.CANCELLED:
                self._store.update_status(
                    child.id, SessionStatus.CANCELLED,
                    error=child_result.error or child_result.summary,
                )
                self._store.set_summary(
                    child.id, child_result.summary, status=SessionStatus.CANCELLED,
                )
            elif child_result is None or child_result.status is ForkStatus.FAILED:
                summary = (
                    child_result.summary if child_result is not None
                    else "Subagent execution failed before producing a result"
                )
                error = (
                    (child_result.error or summary)
                    if child_result is not None else child_error or summary
                )
                self._store.update_status(child.id, SessionStatus.FAILED, error=error)
                self._store.set_summary(
                    child.id, summary, status=SessionStatus.FAILED,
                )
            elif child_result.status is ForkStatus.PARTIAL:
                self._store.set_summary(
                    child.id, child_result.summary, status=SessionStatus.PARTIAL,
                )
            else:
                self._store.set_summary(
                    child.id, child_result.summary, status=SessionStatus.COMPLETED,
                )
            completed_child = self._store.get_session(child.id)
            if completed_child is not None:
                self._emit_subagent_event(
                    EventType.SUBAGENT_STOP,
                    parent_session_id=parent.id,
                    root_session_id=parent.root_id,
                    child_session_id=child.id,
                    agent_name=child_agent_type,
                    status=completed_child.status,
                    fork_result=child_result,
                )
            self._cancellation_tokens.pop(
                (child.id, child.generation), None,
            )

    def _start_background_execution(
        self,
        *,
        parent: "SessionRecord",
        child: "SessionRecord",
        agent_name: str,
        execute: Callable[[], AgentRunResult],
        cleanup: Callable[[], None] | None = None,
    ) -> BackgroundAgentHandle:
        generation = child.generation
        execution_key = (child.id, generation)

        def _execute_background() -> None:
            try:
                execute()
            except BaseException:
                logger.exception("Background subagent %s failed", child.id)
            finally:
                if cleanup is not None:
                    try:
                        cleanup()
                    except Exception:
                        logger.exception("Background subagent cleanup failed for %s", child.id)
                try:
                    completed_child = self._store.get_session(child.id)
                    if completed_child is None:
                        logger.error(
                            "Background subagent session %s disappeared", child.id,
                        )
                    else:
                        notification_result = completed_child.agent_result
                        if notification_result is None:
                            notification_result = AgentRunResult(
                                agent_name=completed_child.agent_name,
                                session_id=completed_child.id,
                                status=AgentRunStatus.from_session_status(
                                    completed_child.status
                                ),
                                summary=completed_child.summary,
                                error=completed_child.error,
                            )
                        self._store.append_agent_notification(
                            AgentCompletionNotification(
                                parent_session_id=parent.id,
                                result=notification_result,
                                generation=generation,
                            )
                        )
                except Exception:
                    logger.exception(
                        "Failed to publish background completion for %s", child.id,
                    )
                finally:
                    with self._background_runs_lock:
                        self._background_runs.pop(execution_key, None)

        thread = threading.Thread(
            target=_execute_background,
            name=f"agent-{child.id}-g{generation}",
            daemon=False,
        )
        with self._background_runs_lock:
            self._background_runs[execution_key] = thread
        thread.start()
        return BackgroundAgentHandle(
            agent_name=agent_name,
            session_id=child.id,
            generation=generation,
        )

    def send_agent_message(
        self,
        *,
        parent_session_id: str,
        child_session_id: str,
        message: str,
        budget_tokens: int,
        parent_max_steps: int,
        cancellation_token: CancellationToken,
        parent_policy: "PhasePolicy",
    ) -> AgentMessageReceipt:
        """Resume a terminal direct child with its complete persisted transcript."""
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        if budget_tokens <= 0 or parent_max_steps <= 0:
            raise ValueError("Resume budget and step limit must be positive")
        if not isinstance(cancellation_token, CancellationToken):
            raise TypeError("cancellation_token must be a CancellationToken")
        from core.policy import PhasePolicy
        from agent.session.task_contract import TaskContract
        if not isinstance(parent_policy, PhasePolicy):
            raise TypeError("parent_policy must be a PhasePolicy")

        parent, child = self._require_direct_child(
            parent_session_id, child_session_id,
        )
        # CC-aligned (subagent S4): allow live steering of running children.
        # Append message to child's session; the child picks it up via
        # runtime_message_source on its next turn.
        if child.status in {SessionStatus.RUNNING, SessionStatus.QUEUED}:
            self._store.append_message(
                child.id,
                LLMMessage(role="user", content=(
                    f"[Parent message from {parent.agent_name}]\n{message.strip()}"
                )),
            )
            logger.info(
                "Live message injected into running child %s (generation %d)",
                child.id, child.generation,
            )
            return AgentMessageReceipt(
                child_session_id=child.id,
                generation=child.generation,
                outcome=AgentMessageOutcome.RESUMED_IN_BACKGROUND,
            )
        if child.workspace_mode is not WorkspaceMode.CURRENT:
            raise ValueError(
                "Resuming a managed worktree requires Batch 7 workspace recovery"
            )
        if child.metadata.get("model_name") != self._backend.model_name:
            raise ValueError("Child model changed since its prior generation")

        parent_definition = self._agent_registry.get(parent.agent_name)
        definition = (
            parent_definition
            if child.agent_kind is AgentKind.FORK
            else self._agent_registry.get(child.agent_name)
        )
        if child.agent_kind is AgentKind.NAMED_SUBAGENT:
            allowed = {
                item.name
                for item in self._agent_registry.delegatable_by(parent_definition)
            }
            if definition.name not in allowed:
                raise ValueError(
                    f"Agent {definition.name!r} is no longer delegatable by "
                    f"{parent.agent_name!r}"
                )

        raw_policy = child.metadata.get("parent_policy")
        if not isinstance(raw_policy, dict):
            raise ValueError("Child resume requires its persisted authority policy")
        effective_policy = PhasePolicy.from_dict(raw_policy).intersect(parent_policy)
        contract = TaskContract.for_subagent(
            definition,
            self._root_agent_config,
            parent_budget_tokens=budget_tokens,
            parent_max_steps=parent_max_steps,
        )
        resumed = self._store.prepare_session_resume(
            child.id,
            LLMMessage(role="user", content=message.strip()),
        )
        request = AgentSpawnRequest.resumed(
            agent_kind=child.agent_kind,
            workspace_mode=child.workspace_mode,
            description=message.strip()[:80],
            prompt=message.strip(),
            definition=(
                definition
                if child.agent_kind is AgentKind.NAMED_SUBAGENT else None
            ),
        )
        child_cancellation = cancellation_token.child()
        self._cancellation_tokens[(child.id, resumed.generation)] = child_cancellation
        child_agent_type = (
            AgentKind.FORK.value
            if child.agent_kind is AgentKind.FORK else definition.name
        )
        self._emit_subagent_event(
            EventType.SUBAGENT_START,
            parent_session_id=parent.id,
            root_session_id=parent.root_id,
            child_session_id=child.id,
            agent_name=child_agent_type,
            status=SessionStatus.RUNNING,
        )
        self._fire_hook(HookContext(
            event=HookEvent.SUBAGENT_START,
            session_id=parent.id,
            agent_id=child.id,
            agent_type=child_agent_type,
        ))
        persisted_messages = self._store.list_messages(child.id)
        execute = lambda: self._execute_child_session(
            parent=parent,
            child=resumed,
            request=request,
            definition=definition,
            parent_definition=parent_definition,
            contract=contract,
            cancellation_token=child_cancellation,
            parent_policy=effective_policy,
            repo_path=self._require_project_scope(parent.repo_path),
            child_agent_type=child_agent_type,
            spawn_context=None,
            persisted_messages=persisted_messages,
        )
        handle = self._start_background_execution(
            parent=parent,
            child=resumed,
            agent_name=definition.name,
            execute=execute,
        )
        return AgentMessageReceipt(
            child_session_id=handle.session_id,
            generation=handle.generation,
            outcome=AgentMessageOutcome.RESUMED_IN_BACKGROUND,
        )

    def wait_for_agent(
        self,
        *,
        parent_session_id: str,
        child_session_id: str,
        timeout_seconds: float,
    ) -> AgentWaitResult:
        """Wait for an in-process child without guessing its external liveness."""
        import math
        if not isinstance(timeout_seconds, (int, float)):
            raise TypeError("timeout_seconds must be numeric")
        if timeout_seconds < 0 or not math.isfinite(timeout_seconds):
            raise ValueError("timeout_seconds must be finite and non-negative")
        _, child = self._require_direct_child(
            parent_session_id, child_session_id,
        )
        terminal = {
            SessionStatus.COMPLETED,
            SessionStatus.PARTIAL,
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
        }
        if child.status in terminal:
            return AgentWaitResult(
                child_session_id=child.id,
                generation=child.generation,
                outcome=AgentWaitOutcome.TERMINAL,
                session_status=child.status,
                result=child.agent_result,
            )
        with self._background_runs_lock:
            thread = self._background_runs.get((child.id, child.generation))
        if thread is None:
            return AgentWaitResult(
                child_session_id=child.id,
                generation=child.generation,
                outcome=AgentWaitOutcome.UNAVAILABLE,
                session_status=child.status,
            )
        thread.join(float(timeout_seconds))
        current = self._store.get_session(child.id)
        if current is None:
            raise ValueError(f"Unknown v2 session: {child.id}")
        outcome = (
            AgentWaitOutcome.TERMINAL
            if current.status in terminal else AgentWaitOutcome.TIMED_OUT
        )
        return AgentWaitResult(
            child_session_id=current.id,
            generation=current.generation,
            outcome=outcome,
            session_status=current.status,
            result=(current.agent_result if outcome is AgentWaitOutcome.TERMINAL else None),
        )

    def cancel_agent(
        self,
        *,
        parent_session_id: str,
        child_session_id: str,
        detail: str = "",
    ) -> AgentCancelResult:
        """Request cooperative cancellation of one direct active child."""
        _, child = self._require_direct_child(
            parent_session_id, child_session_id,
        )
        terminal = {
            SessionStatus.COMPLETED,
            SessionStatus.PARTIAL,
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
        }
        if child.status in terminal:
            return AgentCancelResult(
                child_session_id=child.id,
                generation=child.generation,
                outcome=AgentCancelOutcome.ALREADY_TERMINAL,
                session_status=child.status,
            )
        token = self._cancellation_tokens.get((child.id, child.generation))
        if token is None:
            return AgentCancelResult(
                child_session_id=child.id,
                generation=child.generation,
                outcome=AgentCancelOutcome.UNAVAILABLE,
                session_status=child.status,
            )
        token.cancel(detail=detail)
        return AgentCancelResult(
            child_session_id=child.id,
            generation=child.generation,
            outcome=AgentCancelOutcome.REQUESTED,
            session_status=child.status,
        )

    def _require_direct_child(
        self, parent_session_id: str, child_session_id: str,
    ) -> tuple["SessionRecord", "SessionRecord"]:
        parent = self._store.get_session(parent_session_id)
        if parent is None:
            raise ValueError(f"Unknown parent session: {parent_session_id}")
        self._require_project_scope(parent.repo_path)
        child = self._store.get_session(child_session_id)
        if child is None or child.parent_id != parent.id:
            raise ValueError("Agent session must be a direct child of the caller")
        if child.repo_path != parent.repo_path:
            raise ValueError("Parent and child project roots do not match")
        return parent, child

    def _claim_completion_messages(
        self, parent_session_id: str,
    ) -> list[LLMMessage]:
        """Project typed completion events into parent-visible messages."""
        notifications = self.claim_agent_completions(parent_session_id)
        return self._project_completion_notifications(notifications)

    def claim_agent_completions(
        self, parent_session_id: str,
    ) -> tuple[AgentCompletionNotification, ...]:
        """Claim all pending typed child completions for one parent session."""
        parent = self._store.get_session(parent_session_id)
        if parent is None:
            raise ValueError(f"Unknown parent session: {parent_session_id}")
        self._require_project_scope(parent.repo_path)
        return self._store.claim_pending_agent_notifications(parent_session_id)

    def _project_completion_notifications(
        self, notifications: tuple[AgentCompletionNotification, ...],
    ) -> list[LLMMessage]:
        """Render claimed typed child completions into parent-visible messages."""
        from agent.session.task_tool import _format_fork_result

        return [
            LLMMessage(
                role="user",
                content=_format_fork_result(
                    notification.result.agent_name,
                    notification.result,
                    generation=notification.generation,
                ),
            )
            for notification in notifications
        ]

    # ── Live message injection (subagent S4: live steering) ───────────

    def _claim_new_messages(self, session_id: str) -> list[LLMMessage]:
        """Return messages added to this session since last check.

        CC-aligned (subagent S4): running children pick up parent-injected
        messages on each turn via runtime_message_source.
        Uses DB row id tracking on LLMMessage.db_id (set by list_messages).
        First call seeds the tracker with the max existing id — no messages
        are returned until new ones are appended.
        """
        key = f"_last_msg_id_{session_id}"
        all_msgs = self._store.list_messages(session_id)
        # Find the max existing id
        max_existing = 0
        for msg in all_msgs:
            msg_id = getattr(msg, "db_id", 0) or 0
            if msg_id > max_existing:
                max_existing = msg_id
        # Seed on first call
        last_id = getattr(self, key, None)
        if last_id is None:
            setattr(self, key, max_existing)
            return []
        # Return messages newer than last check
        new_msgs: list[LLMMessage] = []
        for msg in all_msgs:
            msg_id = getattr(msg, "db_id", 0) or 0
            if msg_id > last_id:
                new_msgs.append(msg)
        if new_msgs:
            setattr(self, key, max_existing)
            logger.debug("Live steering: %d new message(s) for session %s", len(new_msgs), session_id)
        return new_msgs

    # ── Internal helpers ──

    def fork_session(
        self,
        *,
        parent_session_id: str,
        definition: AgentDefinition,
        description: str,
        prompt: str,
        budget_tokens: int,
        parent_max_steps: int,
        cancellation_token: CancellationToken,
        parent_policy: "PhasePolicy",
        origin: DelegationOrigin = DelegationOrigin.TOOL,
        spawn_context: AgentSpawnContext | None = None,
    ) -> AgentRunResult:
        """Compatibility entrypoint for a fresh named child."""
        return self.spawn_agent(
            parent_session_id=parent_session_id,
            request=AgentSpawnRequest.named(
                definition=definition,
                description=description,
                prompt=prompt,
            ),
            budget_tokens=budget_tokens,
            parent_max_steps=parent_max_steps,
            cancellation_token=cancellation_token,
            parent_policy=parent_policy,
            origin=origin,
            spawn_context=spawn_context,
        )

    def _fire_hook(self, context: HookContext) -> DispatchResult:
        if self._hook_dispatcher is None:
            return DispatchResult()
        try:
            return self._hook_dispatcher.dispatch(context.event, context)
        except Exception:
            logger.debug(
                "Hook %s failed for session %s",
                context.event.value, context.session_id, exc_info=True,
            )
            return DispatchResult()

    @property
    def hook_dispatcher(self):
        """Lifecycle dispatcher shared by all sessions in this Runtime."""
        return self._hook_dispatcher

    def _emit_subagent_event(
        self,
        event_type: EventType,
        *,
        parent_session_id: str,
        root_session_id: str,
        child_session_id: str,
        agent_name: str,
        status: SessionStatus,
        fork_result: AgentRunResult | None = None,
    ) -> None:
        if self._event_callback is None:
            return
        payload = {
            "parent_session_id": parent_session_id,
            "root_session_id": root_session_id,
            "session_id": child_session_id,
            "agent_name": agent_name,
            "status": status.value,
            "turns_used": fork_result.turns_used if fork_result else 0,
            "tokens_used": fork_result.tokens_used if fork_result else 0,
            "summary": fork_result.summary if fork_result else "",
            "error": fork_result.error if fork_result else "",
        }
        try:
            self._event_callback(Event(
                event_type=event_type,
                task_id=child_session_id,
                payload=payload,
                session_id=parent_session_id,
            ))
        except Exception:
            logger.debug(
                "V2 subagent event callback failed for %s",
                child_session_id, exc_info=True,
            )

    def _build_registry_for_session(
        self, spec: AgentDefinition, session,
    ) -> ToolRegistry:
        """委托给 registry_builder。"""
        from agent.session.registry_builder import build_registry_for_session
        override = session.metadata.get("permission_mode_override", "") if hasattr(session, "metadata") else ""
        return build_registry_for_session(
            spec, session,
            base_registry=self._base_registry,
            agent_registry=self._agent_registry,
            circuit_breaker=self._circuit_breaker,
            runtime=self,
            mcp_tool_names=self._mcp_tool_names_for_spec(spec),
            permission_mode_override=override,
        )

    def _sync_mcp_capabilities(self) -> None:
        """Sync MCP tool states into the capability registry.

        When MCP integration is absent or a server failed to connect,
        mark those tools as UNAVAILABLE so the model never sees them.
        """
        if self._mcp_integration is None:
            return
        mcp_tool_names = getattr(self._mcp_integration, "tool_names", frozenset())
        for name in mcp_tool_names:
            self._capability_registry.register(name)

        # Check for failed MCP servers
        failed_servers = getattr(self._mcp_integration, "failed_servers", None)
        if failed_servers:
            for server_name, reason in failed_servers.items():
                server_tools = getattr(self._mcp_integration, "server_tools", {}).get(server_name, [])
                for tool_name in server_tools:
                    self._capability_registry.mark_unavailable(
                        tool_name, f"MCP server '{server_name}': {reason}",
                    )

    def _resolve_child_permission_mode(
        self, parent: AgentDefinition, child: AgentDefinition | None
    ) -> str:
        """CC-aligned: resolve effective permission_mode for a child subagent.

        CC rules (from Agent SDK permissions docs):
        1. Parent bypassPermissions → child forced bypassPermissions
           (cannot be downgraded by child definition)
        2. Parent acceptEdits/auto → child inherits parent mode
           (child cannot upgrade to bypassPermissions)
        3. Parent plan → child forced plan (read-only)
        4. Parent dontAsk → child inherits dontAsk + parent's allow rules
        5. Otherwise → child uses its own AgentDefinition.permission_mode,
           falling back to parent mode.
        """
        parent_mode = parent.permission_mode or "default"

        # bypassPermissions is the highest privilege — forced inherit
        if parent_mode == "bypassPermissions":
            return "bypassPermissions"

        # plan is read-only — forced inherit
        if parent_mode == "plan":
            return "plan"

        # acceptEdits / auto / dontAsk: child can't upgrade
        if parent_mode in ("acceptEdits", "auto", "dontAsk"):
            child_mode = child.permission_mode if child else ""
            # Child cannot upgrade to bypassPermissions
            if child_mode == "bypassPermissions":
                return parent_mode
            # Use child's mode if set, otherwise inherit parent
            return child_mode or parent_mode

        # default / manual: child uses own config
        child_mode = child.permission_mode if child else ""
        return child_mode or parent_mode

    # ── Headless Web Approval (CC control_request/control_response equivalent) ─

    def _ensure_approval_broker(self, session_id: str) -> "ApprovalBroker":
        """Get or create the per-session ApprovalBroker.

        One broker per session.  The agent thread blocks on
        ``broker.wait_for_decision()``; the HTTP handler resolves via
        ``broker.resolve()``.  This is the exact same synchronous-blocking
        pattern as CC's stdin ``control_response``.
        """
        if session_id not in self._approval_brokers:
            from server.services.approval_broker import ApprovalBroker
            self._approval_brokers[session_id] = ApprovalBroker(session_id)
        return self._approval_brokers[session_id]

    def get_approval_broker(self, session_id: str) -> "ApprovalBroker | None":
        """Return the ApprovalBroker for *session_id*, if one exists."""
        return self._approval_brokers.get(session_id)

    def set_web_confirm_callback(
        self, session_id: str, callback: "WebConfirmCallback",
    ) -> None:
        """Register a web_confirm_callback for the next run of *session_id*.

        Called by agent_service before run_session().  The callback is
        injected into the PermissionPipeline during registry construction.
        """
        self._web_confirm_callbacks[session_id] = callback

    def _mcp_tool_names_for_spec(self, spec: AgentDefinition) -> frozenset[str]:
        if self._mcp_integration is None:
            return frozenset()
        from agent.capability_registry import CapabilityState
        # CC-aligned: resolve named mcpServers references from frontmatter
        if spec.mcp_servers:
            server_tools = self._mcp_integration.server_tools
            raw_names: set[str] = set()
            for entry in spec.mcp_servers:
                if isinstance(entry, str):
                    raw_names.update(server_tools.get(entry, []))
                elif isinstance(entry, dict):
                    # Inline definition — connected at agent start, tools lazy-registered
                    for name in entry:
                        raw_names.update(server_tools.get(name, []))
            return frozenset(
                n for n in raw_names
                if self._capability_registry.state_for(n) is CapabilityState.AVAILABLE
            )
        # Fallback (backward compat): EDIT-intent agents get session-level MCP tools
        if spec.intent is not TaskIntent.EDIT:
            return frozenset()
        raw_names = getattr(self._mcp_integration, "tool_names", frozenset())
        return frozenset(
            n
            for n in raw_names
            if self._capability_registry.state_for(n) is CapabilityState.AVAILABLE
        )

    def _build_agent_config(self, spec: AgentDefinition) -> AgentConfig:
        cfg = copy.copy(self._root_agent_config)
        cfg.circuit_breaker = self._circuit_breaker
        if spec.mode != SessionMode.PRIMARY:
            cfg.max_steps = min(cfg.max_steps, spec.max_turns)
            cfg.compact_history = False
            cfg.stream = False
            cfg.stream_callback = None
            cfg.thought_callback = None
            cfg.token_callback = None
        return cfg

    def _build_runtime_messages(self, spec: AgentDefinition, task_description: str) -> list[LLMMessage]:
        """委托给 runtime_prompt_builder。"""
        from agent.session.runtime_prompt_builder import build_runtime_messages
        skill_registry = getattr(self._base_registry, "_skill_registry", None)
        return build_runtime_messages(
            spec, task_description,
            agent_registry=self._agent_registry,
            project_dir=self._agent_registry.project_dir if self._agent_registry else None,
            skill_registry=skill_registry,
        )


def default_session_db_path(repo_path: str) -> str:
    from core.state_paths import ProjectStatePaths

    return str(ProjectStatePaths.for_project(repo_path).sessions_db)


def memory_freshness_text(name: str, store) -> str:
    """Return a freshness warning for a memory file based on mtime.

    Returns '' for fresh files (<=1 day), relative age warning for older.
    """
    import os as _os
    from datetime import datetime as _datetime

    try:
        path = store._file_path(name)
        if not path.exists():
            return ""
        mtime = _datetime.fromtimestamp(_os.path.getmtime(path))
        age_days = (_datetime.now() - mtime).days
        if age_days <= 1:
            return ""
        return f"{age_days} days ago — verify against current code"
    except Exception:
        return ""

# ── spawn_agent / _execute_child_session (extracted to runtime_spawn.py) ──
from agent.session.runtime_spawn import spawn_agent, _execute_child_session
SessionRuntime.spawn_agent = spawn_agent
SessionRuntime._execute_child_session = _execute_child_session
