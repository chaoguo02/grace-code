"""G41: DEPRECATED — replaced by runtime_core.step_loop.StepLoop + runtime_core.runtime.AgentRuntime (G15-G20).

Session Runtime — fresh-context child-session orchestration.
Kept for backward compat.  Will be deleted in full deletion phase.
"""

from __future__ import annotations

import copy
import logging
import threading
import time as _time
import uuid
from dataclasses import asdict, dataclass, is_dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from agent.core import AgentConfig
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
from agent.session.run_context import (
    AgentSpawnContext, CancellationToken,
)
from agent.session.mode_execution_policy import ModeExecutionPolicy
from context.history import ConversationHistory
from hooks.events import HookContext, HookEvent, SessionStartSource
from hooks.protocol import DispatchResult
from llm.base import LLMBackend, LLMMessage
from core.base import ToolRegistry

logger = logging.getLogger(__name__)


class ExplicitDelegationError(ValueError):
    """An explicit child request cannot be honored by the parent contract."""


@dataclass(frozen=True)
class RuntimeDependencies:
    """Immutable composition contract for SessionRuntime infrastructure."""

    store: SessionStore
    backend: LLMBackend
    base_registry: ToolRegistry
    agent_registry: AgentRegistryV2
    root_agent_config: AgentConfig
    log_dir: str
    memory_context: object | None = None
    hook_dispatcher: object | None = None
    mcp_integration: object | None = None
    event_callback: object | None = None
    governor: object | None = None


def _inject_shared_read_cache(
    registry: ToolRegistry,
    read_cache: object,
) -> tuple[str, ...]:
    """Bind every cache-aware file tool to the Runtime-owned cache.

    Registry aliases are not keys in ``_tools``.  Iterating the registered
    instances avoids silently missing canonical tools such as ``Edit`` whose
    backward-compatible alias is ``file_edit``.
    """
    injected: list[str] = []
    for tool_name, tool in getattr(registry, "_tools", {}).items():
        if hasattr(tool, "_read_cache"):
            tool._read_cache = read_cache
            injected.append(tool_name)
    return tuple(injected)


if TYPE_CHECKING:
    from agent.completion_guard import CompletionCheckResult, CompletionContext
    from agent.session.task_contract import TaskContract
    from core.policy import PhasePolicy
    from agent.session.models import SessionRecord
    from agent.session.worktree_service import WorktreeOperationResult
    from agent.session.worktree_manager import Worktree
    from hitl.pipeline import WebConfirmCallback
    from llm.base import StreamCallback
    from server.services.approval_broker import ApprovalBroker
    from tools.file_tool import FileReadCache


class SessionRuntime:
    """DEPRECATED — Phase 10: replaced by AgentRuntime + NativeStepLoop.

    Web path uses AgentRuntime via ChatPipeline._execute_native() (Phase 0a).
    CLI path supports native via ChatSession(agent_runtime=...) (Phase 10 Batch B).
    Remaining callers: entry/modes/v2_runner.py, agent_service.py callbacks.
    Do not add new dependencies on this class.

    Original docstring: Session runtime with fresh-context subagent orchestration.
    Coordinator agents (build, plan) carry the `task` tool and can dispatch child
    subagents. Each child runs in a fresh context with tools restricted to its
    agent definition allow-list.
    """

    def __init__(
        self,
        *,
        dependencies: RuntimeDependencies | None = None,
        store: SessionStore | None = None,
        backend: LLMBackend | None = None,
        base_registry: ToolRegistry | None = None,
        agent_registry: AgentRegistryV2 | None = None,
        root_agent_config: AgentConfig | None = None,
        log_dir: str | None = None,
        memory_context=None,
        hook_dispatcher=None,
        mcp_integration=None,
        event_callback=None,
        governor=None,
    ) -> None:
        if dependencies is not None:
            if any(value is not None for value in (
                store, backend, base_registry, agent_registry,
                root_agent_config, log_dir, memory_context, hook_dispatcher,
                mcp_integration, event_callback, governor,
            )):
                raise ValueError(
                    "dependencies cannot be combined with legacy constructor arguments"
                )
            store = dependencies.store
            backend = dependencies.backend
            base_registry = dependencies.base_registry
            agent_registry = dependencies.agent_registry
            root_agent_config = dependencies.root_agent_config
            log_dir = dependencies.log_dir
            memory_context = dependencies.memory_context
            hook_dispatcher = dependencies.hook_dispatcher
            mcp_integration = dependencies.mcp_integration
            event_callback = dependencies.event_callback
            governor = dependencies.governor
        if any(value is None for value in (
            store, backend, base_registry, agent_registry,
            root_agent_config, log_dir,
        )):
            raise TypeError("SessionRuntime requires complete RuntimeDependencies")

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
        # ResourceGovernor: set by AgentService. Phase 1+ multi-agent paths
        # call admit()/release() on this. May be None in test contexts.
        self._governor = governor
        provider_governor = getattr(backend, "_provider_governor", None)
        if provider_governor is None and governor is not None:
            from core.provider_governor import ProviderGovernor

            provider_governor = ProviderGovernor(
                getattr(governor, "_config", None),
            )
        if provider_governor is not None:
            from llm.provider_capacity import attach_provider_governor

            attach_provider_governor(backend, provider_governor)
            selector_backend = getattr(
                memory_context, "_selector_backend", None,
            )
            if selector_backend is not None:
                attach_provider_governor(
                    selector_backend,
                    provider_governor,
                )
        # Phase 2: shared bounded executor for multi-agent delegation
        from concurrent.futures import ThreadPoolExecutor as _TPE
        self._shared_executor = _TPE(
            max_workers=getattr(getattr(governor, "_config", None), "worker", None)
            and getattr(getattr(governor, "_config", None).worker, "global_max", 4)
            or 4,
            thread_name_prefix="grace-delegation",
        ) if governor is not None else None
        # Callback for publishing run_terminal / run_started WS events.
        # Set by AgentService.  Signature: (session_id, event_dict) -> None.
        # Unlike _event_callback which receives agent.task.Event objects,
        # this receives pre-formatted WS message dicts ready for broadcast.
        # Per-session ApprovalBroker instances for headless Web mode.
        # CC-aligned: each session has its own blocking approval queue,
        # equivalent to CC's per-session stdin control_request channel.
        self._approval_brokers: dict[str, "ApprovalBroker"] = {}
        # Per-session web_confirm_callback factories, set by agent_service
        # before run_session().  keyed by session_id.
        self._web_confirm_callbacks: dict[str, "WebConfirmCallback"] = {}
        self._stream_callbacks: dict[str, "StreamCallback"] = {}
        self._text_lifecycle_callbacks: dict[str, object] = {}
        self._text_delta_callbacks: dict[str, object] = {}
        self._cancellation_tokens: dict[tuple[str, int], CancellationToken] = {}
        self._backend_store: dict[str, "LLMBackend"] = {}
        # Pre-run Skill activations are isolated by session.  Once a run owns
        # its Store, activations are recorded directly instead of passing
        # through shared mutable "current run" state.
        self._pending_skill_activations: dict[str, list[dict[str, object]]] = {}
        self._active_evidence_stores: dict[str, object] = {}
        self._active_evidence_requirements: dict[str, object] = {}
        from agent.session.run_evidence import EvidenceStoreManager
        self._evidence_stores = EvidenceStoreManager(
            persist_fn=self._store.create_evidence,
            list_fn=self._store.list_evidence,
        )
        # Shared read cache — survives tool registry rebuilds across turns.
        # Without this, PolicyAwareToolRegistry.copy() + scoped() may give
        # Read and Edit tools different _read_cache instances, causing
        # Read-before-Edit failures on multi-turn sessions.
        from tools.file_tool import FileReadCache as _FRC
        self._read_cache: "FileReadCache" = _FRC()
        """Per-session LLM Backend instances.
        Keyed by session_id to eliminate global singleton race conditions.
        When a session_id is not present, the default backend (self._backend)
        is used as a fallback for backward compatibility."""
        self._background_runs: dict[tuple[str, int], threading.Thread] = {}
        self._background_runs_lock = threading.Lock()
        self._spawn_lock = threading.Lock()
        self._spawn_reservations = 0
        # Prevent concurrent execution on the same session (TOCTOU guard).
        self._active_sessions: set[str] = set()
        self._active_sessions_lock = threading.Lock()
        # Set by AgentService to True when running in Web mode.
        # Child agents use this to decide whether to create web callbacks.
        self._is_web_mode: bool = False
        self._session_permission_modes: dict[str, str] = {}
        self._session_injected_rules: dict[str, list] = {}

        # ── Circuit Breaker (code-level, not prompt-based) ──
        from core.circuit_breaker import CircuitBreaker
        self._circuit_breaker = CircuitBreaker()

        # ── P1-6: Dynamic Capability Registry ──
        # Phase 0 rename: CapabilityRegistry → ToolAvailabilityGuard
        # (agent/capability_registry.py was renamed to agent/tool_availability_guard.py).
        from agent.tool_availability_guard import ToolAvailabilityGuard
        self._tool_availability_guard = ToolAvailabilityGuard()
        # Register all builtin tools from the base registry
        self._tool_availability_guard.register_bulk(self._base_registry.tool_names)
        # Wire the guard into the base ToolRegistry for physical interception
        self._base_registry._tool_availability_guard = self._tool_availability_guard
        # Mark MCP tools as UNAVAILABLE if the bridge failed to connect
        self._sync_mcp_capabilities()

    def dispose(self) -> None:
        """Release all mutable state. Called by AgentService.shutdown().

        Phase 2 unified shutdown order:
          1. Cancel all cancellation tokens
          2. Drain background threads (with timeout)
          3. Shutdown shared executor
          4. Clear remaining state

        Idempotent — safe to call multiple times.
        """
        # 1. Cancel all cancellation tokens
        from agent.task import TerminationReason
        for (session_id, gen), token in list(self._cancellation_tokens.items()):
            try:
                token.cancel(
                    TerminationReason.USER_CANCELLED,
                    detail="Runtime disposing",
                )
            except Exception:
                pass

        # 2. Drain background threads (with bounded timeout)
        with self._background_runs_lock:
            threads = list(self._background_runs.values())
        shutdown_cfg = getattr(
            getattr(self._governor, "_config", None), "shutdown", None,
        )
        drain_timeout = max(
            0.0, float(getattr(shutdown_cfg, "drain_timeout_seconds", 30.0)),
        )
        deadline = _time.monotonic() + drain_timeout
        for t in threads:
            remaining = deadline - _time.monotonic()
            if remaining > 0:
                t.join(timeout=remaining)

        # 3. Shutdown shared executor
        if self._shared_executor is not None:
            executor = self._shared_executor
            self._shared_executor = None
            shutdown_done = threading.Event()

            def _shutdown_executor() -> None:
                try:
                    executor.shutdown(wait=True, cancel_futures=True)
                finally:
                    shutdown_done.set()

            shutdown_thread = threading.Thread(
                target=_shutdown_executor,
                name="grace-executor-shutdown",
                daemon=True,
            )
            shutdown_thread.start()
            force_timeout = max(
                0.0, float(getattr(shutdown_cfg, "force_kill_seconds", 5.0)),
            )
            shutdown_done.wait(timeout=force_timeout)
            if not shutdown_done.is_set():
                logger.warning(
                    "Shared executor did not drain within %.1fs",
                    force_timeout,
                )

        # 4. Clear remaining state
        with self._active_sessions_lock:
            backends = list(self._backend_store.values())
            self._active_sessions.clear()
            self._backend_store.clear()
        for backend in backends:
            if backend is self._backend:
                continue
            try:
                backend.close()
            except Exception:
                logger.debug(
                    "Per-session backend close failed", exc_info=True,
                )
        self._approval_brokers.clear()
        self._web_confirm_callbacks.clear()
        self._stream_callbacks.clear()
        self._text_lifecycle_callbacks.clear()
        self._text_delta_callbacks.clear()
        self._cancellation_tokens.clear()

    # ── P1-10 thin adapter methods (used by ChatPipeline) ────────────────

    def set_permission_mode_for_session(
        self, session_id: str, mode: str,
    ) -> None:
        self._session_permission_modes[session_id] = mode

    def set_injected_rules_for_session(
        self, session_id: str, rules: list,
    ) -> None:
        self._session_injected_rules[session_id] = rules

    def pop_pending_permission_mode_override(self, session_id: str) -> str | None:
        return self._session_permission_modes.pop(session_id, None)

    def pop_injected_rules(self, session_id: str) -> list | None:
        return self._session_injected_rules.pop(session_id, None)

    # ── Backend store accessors ──────────────────────────────────────────

    def get_backend_for_session(self, session_id: str) -> "LLMBackend":
        """Return the per-session backend or the default backend.

        Per-session backends are created by AgentService when a model switch
        is pending. If no per-session backend exists for this session_id,
        returns the global default backend (self._backend).
        """
        with self._active_sessions_lock:
            return self._backend_store.get(session_id, self._backend)

    def set_backend_for_session(self, session_id: str, backend: "LLMBackend") -> None:
        """Store a per-session backend for the given session."""
        provider_governor = getattr(
            self._backend, "_provider_governor", None,
        )
        if provider_governor is not None:
            from llm.provider_capacity import attach_provider_governor

            attach_provider_governor(backend, provider_governor)
        with self._active_sessions_lock:
            self._backend_store[session_id] = backend

    def release_backend_for_session(self, session_id: str) -> None:
        """Remove the per-session backend after execution completes."""
        with self._active_sessions_lock:
            self._backend_store.pop(session_id, None)

    @property
    def agent_registry(self) -> AgentRegistryV2:
        return self._agent_registry

    @property
    def session_store(self) -> SessionStore:
        return self._store

    @property
    def root_agent_config(self) -> AgentConfig:
        return self._root_agent_config

    @property
    def governor(self):
        """The service-level ResourceGovernor. May be None in test contexts."""
        return self._governor

    @property
    def circuit_breaker(self):
        return self._circuit_breaker

    @property
    def tool_availability_guard(self):
        return self._tool_availability_guard

    # Public API for external callers

    def get_approval_broker(self, session_id: str):
        """Public accessor for session-scoped approval broker."""
        return self._approval_brokers.get(session_id)

    def cancel_all_sessions(self, reason: str = "shutdown") -> int:
        """Cancel all active sessions.  Returns count of cancelled sessions."""
        count = 0
        for (session_id, gen) in list(self._cancellation_tokens.keys()):
            if self.cancel_session(session_id, detail=reason):
                count += 1
        return count

    def get_active_sessions(self) -> list[str]:
        """Return list of active session IDs."""
        return list(self._active_sessions)

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

    def try_acquire_session(self, session_id: str) -> bool:
        """Atomically mark a session as running. Returns False if already active.

        This closes the TOCTOU window between the status check in the HTTP
        handler and the background thread spawn in run_chat_async.
        """
        with self._active_sessions_lock:
            if session_id in self._active_sessions:
                return False
            self._active_sessions.add(session_id)
            return True

    def release_session(self, session_id: str) -> None:
        """Mark a session as no longer running."""
        with self._active_sessions_lock:
            self._active_sessions.discard(session_id)

    def cleanup_session(self, session_id: str) -> None:
        """Release all runtime resources associated with a session.

        Callers (e.g. HTTP delete handler) must use this instead of reaching
        into runtime internals like _approval_brokers, _web_confirm_callbacks,
        or _cancellation_tokens directly.
        """
        # 1. Cancel any running execution
        self.cancel_session(session_id)

        # 2. Clean up approval broker (prevents memory leak)
        self._approval_brokers.pop(session_id, None)

        # 3. Clean up web confirm callback
        self._web_confirm_callbacks.pop(session_id, None)

        # 4. Clean up stream callback
        self._stream_callbacks.pop(session_id, None)

        # 5. Clean up cancellation tokens for this session and its children
        keys_to_remove = [
            k for k in self._cancellation_tokens if k[0] == session_id
        ]
        for k in keys_to_remove:
            self._cancellation_tokens.pop(k, None)

        # 6. Release TOCTOU guard
        self.release_session(session_id)

        # 7. Release per-session backend (prevents memory leak after crash recovery)
        self.release_backend_for_session(session_id)

    def _require_project_scope(self, repo_path: str) -> str:
        """Normalize and verify a repo against this Runtime's registry scope."""
        normalized = str(Path(repo_path).expanduser().resolve())
        if self._agent_registry.project_dir != normalized:
            raise ValueError(
                "Agent registry project scope does not match the execution repo: "
                f"registry={self._agent_registry.project_dir!r}, repo={normalized!r}"
            )
        return normalized

    def _require_review_snapshot_scope(
        self,
        parent_repo_path: str,
        snapshot_repo_path: str,
    ) -> str:
        """Allow an execution root only inside this project's managed snapshots."""
        parent_repo = self._require_project_scope(parent_repo_path)
        from core.state_paths import ProjectStatePaths

        root = ProjectStatePaths.for_project(parent_repo).review_snapshots.resolve()
        target = Path(snapshot_repo_path).expanduser().resolve()
        try:
            relative = target.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                "Review execution repo is outside managed runtime state"
            ) from exc
        if len(relative.parts) != 1 or not target.is_dir():
            raise ValueError("Review execution repo is not a materialized snapshot")
        return str(target)

    def get_session_repo_path(self, session_id: str) -> str:
        """Return a verified parent-session project root or fail closed."""
        session = self._store.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
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
        self._record_delegation_integration(
            child.id,
            "applied" if result.is_success else result.status.value,
            result.error,
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
        self._record_delegation_integration(
            child.id, result.status.value, result.error,
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
            operation = WorktreeOperationResult(
                WorktreeOperationStatus.FAILED,
                evidence,
                evidence.error or "Unable to inspect child worktree",
            )
        elif evidence.revision != expected_revision:
            operation = WorktreeOperationResult(
                WorktreeOperationStatus.STALE,
                evidence,
                "Child worktree revision changed after review",
            )
        else:
            self._store.set_agent_result(
                child.id,
                replace(
                    fork_result,
                    worktree=evidence,
                    worktree_disposition=WorktreeDisposition.RETAINED,
                ),
            )
            operation = WorktreeOperationResult(
                WorktreeOperationStatus.RETAINED, evidence,
            )
        self._record_delegation_integration(
            child.id, operation.status.value, operation.error,
        )
        return operation

    def _record_delegation_integration(
        self, child_session_id: str, status: str, error: str = "",
    ) -> None:
        task = self._store.get_delegation_task_for_child(child_session_id)
        if task is None:
            return
        normalized = {
            "no_changes": "applied",
            "parent_dirty": "conflict",
            "failed": "conflict",
        }.get(status, status)
        self._store.update_delegation_task(
            str(task["id"]),
            status=str(task["status"]),
            integration_status=normalized,
            integration_error=error,
        )
        self._store.reconcile_delegation_run(str(task["delegation_run_id"]))

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
        """Block success until delegated work and child worktrees converge."""
        from agent.completion_guard import CompletionCheckResult

        pending_worktrees = []
        for child in self._store.list_child_sessions(session_id):
            result = child.agent_result
            if (
                result is not None
                and result.worktree_disposition is WorktreeDisposition.PRESERVED
                and result.worktree is not None
            ):
                pending_worktrees.append((child.id, result.worktree))
        incomplete_runs = [
            run for run in self._store.list_delegation_runs(session_id)
            if str(run.get("status")) == "running"
        ]
        if not pending_worktrees and not incomplete_runs:
            return CompletionCheckResult(can_complete=True)

        sections = []
        if pending_worktrees:
            facts = "\n".join(
                f"- {child_id}: path={evidence.path}, rev={evidence.revision}"
                for child_id, evidence in pending_worktrees
            )
            child_list = ", ".join(cid for cid, _ in pending_worktrees)
            sections.append(
                "Subagent worktrees still need an explicit apply or discard "
                f"decision:\n{facts}\nChild sessions: {child_list}"
            )
        if incomplete_runs:
            facts = "\n".join(
                f"- {run['id']}: status={run['status']}, phase={run['phase']}"
                for run in incomplete_runs
            )
            sections.append(
                "Delegation runs have not met their required-task gate. Retry or "
                f"cancel/resolve the incomplete work:\n{facts}"
            )
        return CompletionCheckResult(
            can_complete=False,
            blocked_reason="Unresolved multi-agent delegation",
            inject_message=(
                "[RUNTIME BLOCK] Multi-agent work has not converged:\n"
                + "\n\n".join(sections)
            ),
        )

    def add_completion_verifier(
        self, verifier: "Callable[[CompletionContext], CompletionCheckResult | None]",
    ) -> None:
        """Register an external completion condition.

        Verifiers run after the built-in checks (git diff, worktree disposition).
        Return a CompletionCheckResult to block completion, or None to pass.
        The *verifier* receives the CompletionContext (files read/written, etc.).
        """
        if not hasattr(self, '_completion_verifiers'):
            self._completion_verifiers: list = []
        self._completion_verifiers.append(verifier)

    # ── Worktree resolution (Gap 15, async queue) ─────────────────────

    def set_worktree_completion_callback(
        self, callback: "Callable[[str, str, str, str], None]",
    ) -> None:
        """Register a callback for worktree resolution completion.

        Called as callback(parent_session_id, child_session_id, action, status).
        The server layer injects WS event publishing here — Runtime stays
        agnostic of the transport layer.
        """
        self._worktree_completion_callback = callback

    def _ensure_worktree_worker(self) -> None:
        """Start the background worktree resolution worker if not running."""
        if getattr(self, '_worktree_worker_started', False):
            return
        import queue as _q
        import threading as _th
        self._worktree_queue: _q.Queue = _q.Queue()
        self._worktree_results: dict[str, dict] = {}
        self._worktree_worker_started = True

        def _worker():
            _logger = logging.getLogger(__name__)
            while True:
                try:
                    cmd = self._worktree_queue.get()
                    if cmd is None:
                        break
                    parent_id, child_id, action, expected_revision = cmd
                    cmd_key = f"{child_id}_{action}"
                    self._worktree_results[cmd_key] = {
                        "status": "processing", "child_session_id": child_id,
                        "action": action,
                        "expected_revision": expected_revision,
                    }
                    result = self._resolve_worktree_sync(
                        parent_id,
                        child_id,
                        action,
                        expected_revision=expected_revision,
                    )
                    self._worktree_results[cmd_key] = result
                    # Notify server layer via injected callback — clean layering.
                    _cb = getattr(self, '_worktree_completion_callback', None)
                    if _cb is not None:
                        try:
                            _cb(parent_id, child_id, action, result.get("status", "error"))
                        except Exception:
                            _logger.debug("Worktree callback failed", exc_info=True)
                except Exception:
                    _logger.exception("Worktree worker error")

        _t = _th.Thread(target=_worker, daemon=True, name="worktree-worker")
        _t.start()

    def enqueue_worktree_command(
        self,
        parent_session_id: str,
        child_session_id: str,
        action: str,
        *,
        expected_revision: str,
    ) -> str:
        """Enqueue a worktree command for async processing. Returns command_key.

        Idempotent: duplicate (child_id, action) pairs are rejected.
        """
        self._ensure_worktree_worker()
        if action not in {"apply", "discard", "retain"}:
            raise ValueError(f"Unsupported worktree action: {action}")
        if not isinstance(expected_revision, str) or not expected_revision.strip():
            raise ValueError("expected_revision is required")
        expected_revision = expected_revision.strip()
        cmd_key = f"{child_session_id}_{action}"
        _existing = getattr(self, '_worktree_results', {}).get(cmd_key)
        if (
            _existing
            and _existing.get("expected_revision") == expected_revision
            and _existing.get("status") in {
                "queued", "processing", "applied", "discarded",
                "retained", "no_changes",
            }
        ):
            return cmd_key  # already enqueued — idempotent
        self._worktree_queue.put(
            (parent_session_id, child_session_id, action, expected_revision)
        )
        self._worktree_results[cmd_key] = {
            "status": "queued", "child_session_id": child_session_id,
            "action": action,
            "expected_revision": expected_revision,
        }
        return cmd_key

    def get_worktree_command_status(self, child_session_id: str, action: str) -> dict | None:
        return getattr(self, '_worktree_results', {}).get(f"{child_session_id}_{action}")

    def _resolve_worktree_sync(
        self,
        parent_session_id: str,
        child_session_id: str,
        action: str,
        *,
        expected_revision: str,
    ) -> dict:
        """Resolve one reviewed revision through the canonical safe methods."""
        if action == "apply":
            operation = self.apply_subagent_worktree(
                parent_session_id,
                child_session_id,
                expected_revision=expected_revision,
            )
        elif action == "discard":
            operation = self.discard_subagent_worktree(
                parent_session_id,
                child_session_id,
                expected_revision=expected_revision,
            )
        elif action == "retain":
            operation = self.retain_subagent_worktree(
                parent_session_id,
                child_session_id,
                expected_revision=expected_revision,
            )
        else:
            raise ValueError(f"Unsupported worktree action: {action}")

        return {
            "resolved": operation.is_success,
            "action": action,
            "child_session_id": child_session_id,
            "status": operation.status.value,
            "message": operation.error or operation.status.value,
            "expected_revision": expected_revision,
            "current_revision": operation.evidence.revision,
        }

    def resolve_worktree(
        self, parent_session_id: str, child_session_id: str,
        action: str,  # "apply" | "discard" | "retain"
        *,
        expected_revision: str,
    ) -> dict:
        """Resolve a preserved child worktree with the given action.

        CC-aligned: worktree operations are Runtime-mediated to ensure
        thread safety and proper filesystem access.

        Returns a status dict with:
          - resolved: bool
          - action: str
          - child_session_id: str
          - status: "applied" | "discarded" | "retained" | "error"
          - message: str
        """
        return self._resolve_worktree_sync(
            parent_session_id,
            child_session_id,
            action,
            expected_revision=expected_revision,
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
        execution_repo_path: str | None = None,
        child_metadata: dict[str, object] | None = None,
        child_created_callback=None,
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
        if execution_repo_path is not None:
            execution_repo_path = self._require_review_snapshot_scope(
                parent.repo_path,
                execution_repo_path,
            )
        if child_metadata is not None and not isinstance(child_metadata, dict):
            raise TypeError("child_metadata must be a dict when provided")
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
        _exp_mode = _derive_product_mode(parent_definition.name)
        _exp_policy = ModeExecutionPolicy.for_run(
            product_mode=_exp_mode,
            primary_agent=parent_definition.name,
            governor=self._governor,
        )
        parent_registry = self._build_registry_for_session(
            parent_definition, parent, mode_policy=_exp_policy,
        )
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
            execution_repo_path=execution_repo_path,
            child_metadata=child_metadata,
            child_created_callback=child_created_callback,
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
        product_mode: str = "",
        request_skill: str = "",
        skill_arguments: str = "",
        messages: list[LLMMessage] | None = None,
        max_steps_override: int | None = None,        # deprecated: use contract
        budget_tokens_override: int | None = None,    # deprecated: use contract
        contract: "TaskContract | None" = None,
        inject_rules: list | None = None,              # Web: permission rules from settings
        inject_permission_mode: str | None = None,     # Web: "acceptEdits" / "default" / etc.
        session_context_text: str = "",                 # Web: session summary for Runtime injection (NOT persisted)
        allowed_prompts: list[dict[str, str]] | None = None,  # CC: ExitPlanMode pre-approvals
        effort_override: str = "",
        skill_modifier: Any = None,
        run_context: Any = None,  # RunContext from POST /chat transaction
    ) -> RunResult:
        session = self._store.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
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

        # ── Run lifecycle: QUEUED → RUNNING ──
        _run_ctx = run_context
        if _run_ctx is not None:
            try:
                _run_id = getattr(_run_ctx, "run_id", None)
                if _run_id:
                    _updated = self._store.start_run_with_event(
                        _run_id,
                        getattr(_run_ctx, "session_id", session_id),
                        turn_id=getattr(_run_ctx, "turn_id", ""),
                        turn_index=getattr(_run_ctx, "turn_index", 0),
                    )
                    if not _updated:
                        logger.warning("Run %s CAS transition queued→running failed", _run_id)
            except Exception:
                logger.debug("Run lifecycle transition skipped", exc_info=True)

        _evidence_store = None
        try:
            # ── Session memory tracker ──
            session_memory_tracker = None
            if self._root_agent_config is not None and self._root_agent_config.session_notes:
                from memory.session_memory import SessionMemoryTracker
                _notes_dir = Path(session.repo_path) / ".grace" / "sessions" / session_id
                _notes_dir.mkdir(parents=True, exist_ok=True)
                _notes_path = _notes_dir / "session_notes.md"
                session_memory_tracker = SessionMemoryTracker(
                    backend=self._backend,
                    notes_path=_notes_path,
                    session_title=f"Session {session_id[:8]}",
                )

            # Inject shared read cache into base registry tools before each run.
            # The PolicyAwareToolRegistry shares tool instances from the base,
            # so all Read/Edit/Write/View tools in the run get the same cache.
            _inject_shared_read_cache(self._base_registry, self._read_cache)

            # ── ModeExecutionPolicy — per-Run immutable execution contract ──
            _resolved_product_mode = product_mode or _derive_product_mode(
                _effective_agent,
            )
            _mode_policy = ModeExecutionPolicy.for_run(
                product_mode=_resolved_product_mode,
                primary_agent=_effective_agent,
                governor=self._governor,
            )

            # ── RunEvidenceStore — one namespace per REAL top-level run ──
            import uuid as _run_uuid
            _root_run_id = str(
                getattr(_run_ctx, "run_id", "") or f"local-{_run_uuid.uuid4()}"
            )
            _root_session_id = session.root_id or session.id
            _evidence_store = self._evidence_stores.acquire(
                _root_run_id,
                root_session_id=_root_session_id,
                turn_id=str(getattr(_run_ctx, "turn_id", "") or ""),
                default_session_id=session_id,
            )
            self._active_evidence_stores[session_id] = _evidence_store

            # ── Wire WS event callback for live evidence streaming ──
            if self._event_callback is not None:
                _evidence_store._event_callback = lambda entry: (
                    self._publish_evidence_event(entry, session_id)
                )

            # ── Flush pending skill activations into evidence ──
            _pending_activations = self._pending_skill_activations.pop(
                session_id, [],
            )
            _flush_skill_activations(_evidence_store, _pending_activations)
            if self._mcp_integration is not None:
                self._mcp_integration.record_run_exposure(
                    _evidence_store, session_id,
                )

            # ── RunEvidenceRequirements — built from trusted sources only ──
            from agent.session.evidence_requirements import (
                RunEvidenceRequirementsFactory,
            )
            # Skill metadata for MCP dep resolution
            _skill_meta = None
            _skill_args = skill_arguments
            if request_skill and self._base_registry.skill_registry is not None:
                try:
                    _skill_meta_raw = self._base_registry.skill_registry.get_skill_meta(
                        request_skill,
                    )
                    if _skill_meta_raw is not None:
                        _skill_meta = {
                            "name": getattr(_skill_meta_raw, "name", request_skill),
                            "mcp_dependencies": sorted(
                                getattr(_skill_meta_raw, "mcp_servers", frozenset())
                            ),
                            "argument_names": tuple(
                                getattr(_skill_meta_raw, "arguments", ())
                            ),
                            "evidence_contract": dict(
                                getattr(_skill_meta_raw, "evidence_contract", {})
                                or {}
                            ),
                            "source": getattr(_skill_meta_raw, "source", ""),
                        }
                except Exception:
                    pass
            # Plan contract from previous Plan run (persisted in session metadata)
            _plan_contract = (session.metadata or {}).get("plan_contract")
            _evidence_requirements = RunEvidenceRequirementsFactory.for_run(
                request_skill=request_skill or None,
                skill_arguments=_skill_args,
                skill_metadata=_skill_meta,
                plan_contract=_plan_contract if isinstance(_plan_contract, dict) else None,
                delegation_plan=None,
                mode_policy=_mode_policy,
            )
            self._active_evidence_requirements[_root_run_id] = _evidence_requirements

            from agent.session.agent_factory import AgentFactory
            _effective_backend = self.get_backend_for_session(session_id)
            _assembly = AgentFactory.create(
                agent_name=_effective_agent,
                backend=_effective_backend,
                base_registry=self._base_registry,
                agent_registry=self._agent_registry,
                root_agent_config=self._root_agent_config,
                memory_context=self._memory_context,
                session=session,
                circuit_breaker=self._circuit_breaker,
                runtime=self,
                mode_policy=_mode_policy,
                mcp_tool_names=self._mcp_tool_names_for_spec(
                    self._agent_registry.get(_effective_agent)
                ),
                session_memory_tracker=session_memory_tracker,
            )
            spec = _assembly.spec
            if spec.completion_requires:
                from agent.session.run_evidence import RequiredToolCall
                _definition_calls = tuple(
                    RequiredToolCall(
                        tool=str(tool_name),
                        minimum_count=max(1, int(minimum)),
                        producer_session_id=session_id,
                    )
                    for tool_name, minimum
                    in spec.completion_requires.items()
                )
                _evidence_requirements = replace(
                    _evidence_requirements,
                    required_tool_calls=(
                        *_evidence_requirements.required_tool_calls,
                        *_definition_calls,
                    ),
                )
                self._active_evidence_requirements[
                    _root_run_id
                ] = _evidence_requirements
            if self._memory_context is not None and hasattr(self._memory_context, "set_session_context"):
                self._memory_context.set_session_context(
                    session_id=session_id,
                    agent_name=_effective_agent,
                    mode=getattr(spec, "mode", ""),
                    repo_path=session.repo_path,
                    session_title=session.title,
                )
                # Set run_id for memory source attribution
                run_id = f"{session_id}-r{session.generation}"
                if hasattr(self._memory_context, "set_run_id"):
                    self._memory_context.set_run_id(run_id)
            effective_intent = TaskIntent(intent) if intent is not None else spec.intent
            _eff_contract = contract if contract is not None else _assembly.contract
            agent = _assembly.agent
            agent_cfg = _assembly.agent_cfg
            agent_cfg.evidence_store = _evidence_store
            agent_cfg.evidence_requirements = _evidence_requirements
            # ── EvidenceScope: artifact dependency boundary ──
            from agent.session.evidence_scope import EvidenceScope
            agent_cfg.evidence_scope = EvidenceScope(
                required_tool_calls=_evidence_requirements.required_tool_calls,
            )
            if effort_override:
                agent_cfg.effort = effort_override
            if skill_modifier is not None:
                apply_modifier = getattr(
                    agent._full_registry,
                    "_apply_skill_modifier",
                    None,
                )
                if callable(apply_modifier):
                    apply_modifier(skill_modifier)

            # ── Inject stream_callback for real-time thought streaming ──
            _stream_cb = self._stream_callbacks.pop(session_id, None)
            if _stream_cb is not None:
                agent_cfg.stream_callback = _stream_cb
            # ── Inject text stream callbacks for assistant_text_start/delta/end ──
            _text_lifecycle_cb = self._text_lifecycle_callbacks.pop(session_id, None)
            _text_delta_cb = self._text_delta_callbacks.pop(session_id, None)
            if _text_lifecycle_cb is not None:
                agent_cfg.text_stream_lifecycle_callback = _text_lifecycle_cb
            if _text_delta_cb is not None:
                agent_cfg.text_stream_delta_callback = _text_delta_cb

            # ── Inject web_confirm_callback into the PermissionPipeline ──
            # CC-aligned: in headless Web mode, the pipeline's Layer 6
            # blocks on threading.Event instead of stdin.  The callback,
            # rules, and permission mode are passed as explicit parameters
            # (rather than shared instance attributes) so concurrent
            # sessions cannot interfere.
            from hitl.pipeline import PermissionSessionConfig

            _web_cb = self._web_confirm_callbacks.pop(session_id, None)
            agent._full_registry.configure_permission_session(
                PermissionSessionConfig(
                    mode=inject_permission_mode,
                    rules=tuple(inject_rules or ()),
                    web_confirm_callback=_web_cb,
                    requesting_agent=spec.name,
                    session_id=session_id,
                    circuit_breaker=agent_cfg.circuit_breaker,
                    approved_prompts=tuple(allowed_prompts or ()),
                    write_allowed=_mode_policy.write_allowed,
                ),
            )

            agent_cfg.cancellation_token = cancellation_token
            agent_cfg.completion_fact_check = (
                lambda: self._check_session_completion(session_id)
            )
            # CC-aligned plan mode throttling: full injection on turn 1,
            # sparse reminder every 5 turns, full re-injection every 25 turns.
            # Delegates to PlanModeAttachmentManager for CC-aligned constants.
            _base_msg_source = lambda: (
                self._claim_completion_messages(session_id)
                + self._claim_new_messages(session_id)
            )
            if spec.permission_mode == "plan":
                from agent.plan_attachment_manager import PlanModeAttachmentManager
                _plan_mgr = PlanModeAttachmentManager()
                _plan_step = [0]
                def _plan_throttled_source():
                    _plan_step[0] += 1
                    _plan_mgr.set_turn(_plan_step[0])
                    _msgs = list(_base_msg_source())
                    attachment = _plan_mgr.get_attachment()
                    if attachment is not None:
                        from llm.base import LLMMessage
                        _msgs.append(LLMMessage(role="user", content=attachment))
                    return _msgs
                agent_cfg.runtime_message_source = _plan_throttled_source
                # Store ref for compaction-triggered refresh
                agent_cfg._plan_attachment_manager = _plan_mgr
            else:
                agent_cfg.runtime_message_source = _base_msg_source
            agent_cfg.stop_hook_event = HookEvent.STOP
            agent_cfg.hook_session_id = session_id
            agent_cfg.hook_agent_id = ""
            agent_cfg.hook_agent_type = spec.name
            # Use the session-scoped dispatcher (created by registry_builder
            # with agent-specific hooks included), NOT the global dispatcher.
            # The session-scoped one lives on the inner tool registry.
            _inner_registry = getattr(agent._full_registry, "_base", None)
            agent_cfg.hook_dispatcher = (
                _inner_registry.hook_dispatcher
                if _inner_registry is not None
                else self._hook_dispatcher
            )
            agent_cfg.stats_session_id = session_id
            agent_cfg.stats_run_id = str(
                getattr(_run_ctx, "run_id", "") or session_id
            )
            agent_cfg.stats_turn_id = str(
                getattr(_run_ctx, "turn_id", "") or ""
            )
            agent_cfg.stats_agent_name = _effective_agent
            agent_cfg.stats_collector = getattr(self, '_stats_recorder', None)
            _memory_event_callback = getattr(self, '_memory_event_callback', None)
            agent_cfg.memory_event_callback = (
                (lambda memory, source, sid=session_id: _memory_event_callback(sid, memory, source))
                if _memory_event_callback is not None else None
            )

            persisted_all = self._store.list_messages(session_id)
            had_persisted_messages = bool(persisted_all)
            _submitted_turn_id = (
                str(getattr(_run_ctx, "turn_id", "") or "")
                if _run_ctx is not None
                else ""
            )
            _prompt_already_persisted = bool(
                _submitted_turn_id
                and any(
                    message.role == "user"
                    and str(getattr(message, "turn_id", "") or "")
                    == _submitted_turn_id
                    for message in persisted_all
                )
            )
            if not _prompt_already_persisted:
                if messages:
                    for message in messages:
                        self._store.append_message(session_id, message)
                else:
                    self._store.append_message(
                        session_id,
                        LLMMessage(role="user", content=task_description),
                    )

            history = ConversationHistory(max_messages=agent_cfg.history_max_messages)
            injected_messages = self._build_runtime_messages(
                spec, task_description, session_id=session_id,
            )
            # Preloaded skills are discovered while building runtime messages.
            # record_skill_activation() writes them directly to the active Store.

            # ── Phase 3A: 断点续传 — 存在有效 RESUME_MARKER 时注入续传提示 ──
            # 进程重启后：若 workspace 未变且有 marker，告知 LLM 从断点继续，
            # 而非重做已完成 turns（宁可重跑，不可错误跳过 R-D）。
            from agent.session.run_evidence import evaluate_resume
            _resume_msg = evaluate_resume(self._store, session_id, session.repo_path)
            if _resume_msg:
                injected_messages.append(
                    LLMMessage(role="system", content=_resume_msg),
                )

            # ── Evidence summary for compaction-aware context ──
            from agent.session.run_evidence import build_evidence_projection
            _evidence_summary = build_evidence_projection(
                _evidence_store, _evidence_requirements,
            )
            if _evidence_summary:
                injected_messages.append(
                    LLMMessage(role="system", content=_evidence_summary),
                )

            # Session context: injected as a Runtime message so the model
            # sees it but it is NOT persisted to DB (filtered by the
            # _RUNTIME_PREFIXES blacklist at persist time).
            if session_context_text:
                injected_messages.append(
                    LLMMessage(role="user", content=session_context_text),
                )

            # ── Context injection uses read-time-truncated messages ──
            # list_messages_for_context() applies tool-output cap,
            # intermediate-thought cap, and 8K-token budget.
            # list_messages() (used by GET /messages) returns FULL content.
            persisted_for_context = self._store.list_messages_for_context(session_id)
            if _prompt_already_persisted:
                # The Run/Turn submission transaction stores the display
                # prompt exactly once. If hooks, @mentions, or Skills changed
                # the execution prompt, replace only the in-memory copy for
                # this model call; keep the durable user-visible text intact.
                for index in range(len(persisted_for_context) - 1, -1, -1):
                    message = persisted_for_context[index]
                    if (
                        message.role == "user"
                        and str(getattr(message, "turn_id", "") or "")
                        == _submitted_turn_id
                    ):
                        if str(message.content or "") != task_description:
                            replacement = LLMMessage(
                                role="user",
                                content=task_description,
                            )
                            replacement.turn_id = _submitted_turn_id
                            persisted_for_context[index] = replacement
                        break
            history.add_many(injected_messages + persisted_for_context)
            agent._pending_history = history

            task = Task(
                task_id=session_id,
                description=task_description,
                repo_path=session.repo_path,
                intent=effective_intent,
                max_steps=(max_steps_override or _eff_contract.max_steps if _eff_contract else agent_cfg.max_steps),
                budget_tokens=(budget_tokens_override or _eff_contract.budget_tokens if _eff_contract else agent_cfg.budget_tokens),
                metadata={
                    "entrypoint": "session",
                    "mode": agent_name,
                    "session_id": session_id,
                    "run_id": str(getattr(_run_ctx, "run_id", "") or ""),
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
            # Track pre-run content fingerprints BEFORE agent.run() so we can
            # detect genuinely new messages afterward, even when auto-compaction
            # (snip/micro-compact) rebuilds history objects in-place.
            import hashlib

            _RUNTIME_PREFIXES = ("[TASK ANCHOR]", "[ENVIRONMENT]", "[PRELOADED SKILLS]",
                                 "[AGENT MEMORY]", "[TASK MODE]", "[ACTIVE POLICY]",
                                 "[FEEDBACK]", "[PREVIOUS SESSION CONTEXT]",
                                 "[SYSTEM]", "[MEMORY RESTORED]",
                                 "[ACCUMULATED FINDINGS]", "[PLAN CONTEXT]",
                                 "[Conversation compacted",
                                 "[Earlier conversation summarized")

            def _msg_fingerprint(msg: LLMMessage) -> str:
                """Stable content fingerprint — survives object recreation."""
                core = f"{msg.role}:{msg.content}:{msg.tool_call_id or ''}"
                return hashlib.sha256(core.encode("utf-8")).hexdigest()[:16]

            _pre_run_fingerprints = {_msg_fingerprint(m) for m in history.to_list()}

            with EventLog.create(task, log_dir=self._log_dir) as log:
                if self._event_callback is not None:
                    original_append = log._append
                    _captured_session_id = session_id
                    _captured_run_ctx = _run_ctx  # capture for turn_id injection

                    def _append_and_emit(event):
                        event.session_id = _captured_session_id
                        original_append(event)
                        try:
                            self._event_callback(event, run_context=_captured_run_ctx)
                        except Exception:
                            logger.debug("Event callback failed", exc_info=True)

                    log._append = _append_and_emit
                result = agent.run(task, log)

            # Persist new history messages and the final assistant answer.
            # Wrap in try/except — the session may have been deleted by a
            # concurrent DELETE handler while the agent was running.
            try:
                _tid = getattr(_run_ctx, "turn_id", "") if _run_ctx else ""
                for message in history.to_list():
                    if _msg_fingerprint(message) in _pre_run_fingerprints:
                        continue
                    content = str(message.content or "")
                    if any(content.startswith(p) for p in _RUNTIME_PREFIXES):
                        continue
                    if _tid:
                        message.turn_id = _tid
                    self._store.append_message(session_id, message)

                if result is not None and result.summary:
                    _amsg = LLMMessage(role="assistant", content=result.summary)
                    if _tid:
                        _amsg.turn_id = _tid
                    self._store.append_message(session_id, _amsg)
            except ValueError as store_error:
                logger.warning(
                    "Session %s was deleted before messages could be persisted — "
                    "this is expected if the session was concurrently removed: %s",
                    session_id[:8], store_error,
                )

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
            # The session may have been deleted by a concurrent DELETE handler
            # while the agent thread was still in this method (cooperative
            # cancellation does not kill the thread).  When the session is gone,
            # log a warning and skip persistence — don't crash.
            try:
                if result is not None:
                    if result.status is RunStatus.CANCELLED:
                        self._store.update_status(
                            session_id, SessionStatus.CANCELLED,
                            error=result.error or result.summary,
                        )
                        if result.summary:
                            self._store.set_summary(
                                session_id, result.summary,
                                status=SessionStatus.CANCELLED,
                            )
                        # ── Run lifecycle: CANCELLED ──
                        self._finalize_run(_run_ctx, result, "cancelled",
                                          error=result.error or result.summary)
                    elif result.status is RunStatus.MAX_STEPS:
                        self._store.set_summary(
                            session_id, result.summary,
                            status=SessionStatus.PARTIAL,
                        )
                        self._finalize_run(_run_ctx, result, "partial")
                    elif result.status is RunStatus.GAVE_UP:
                        self._store.update_status(
                            session_id, SessionStatus.FAILED,
                            error=result.error or result.summary,
                        )
                        if result.summary:
                            self._store.set_summary(
                                session_id, result.summary,
                                status=SessionStatus.FAILED,
                            )
                        self._finalize_run(_run_ctx, result, "gave_up",
                                          error=result.error or result.summary)
                    elif result.status is RunStatus.BLOCKED:
                        self._store.update_status(
                            session_id, SessionStatus.FAILED,
                            error=result.error or result.summary,
                        )
                        if result.summary:
                            self._store.set_summary(
                                session_id, result.summary,
                                status=SessionStatus.FAILED,
                            )
                        self._finalize_run(_run_ctx, result, "blocked",
                                          error=result.error or result.summary)
                    elif result.is_success():
                        self._store.set_summary(
                            session_id, result.summary, status=SessionStatus.COMPLETED,
                        )
                        # Persist plan contract into session metadata
                        _contract = getattr(result, "contract", None)
                        if isinstance(_contract, dict) and _contract:
                            try:
                                self._store.update_metadata(
                                    session_id,
                                    {"plan_contract": _contract},
                                )
                            except Exception:
                                pass
                        # ── Run lifecycle: COMPLETED ──
                        self._finalize_run(_run_ctx, result, "completed")
                    else:
                        self._store.update_status(
                            session_id,
                            SessionStatus.FAILED,
                            error=result.error or result.summary,
                        )
                        if result.summary:
                            self._store.set_summary(
                                session_id, result.summary,
                                status=SessionStatus.FAILED,
                            )
                        # ── Run lifecycle: FAILED ──
                        self._finalize_run(_run_ctx, result, "failed",
                                          error=result.error or result.summary)
                elif cancellation_token.is_cancelled:
                    detail = cancellation_token.detail
                    self._store.update_status(
                        session_id, SessionStatus.CANCELLED, error=detail,
                    )
                    self._finalize_run(_run_ctx, None, "cancelled", error=detail)
                elif execution_error is not None:
                    detail = str(execution_error) or type(execution_error).__name__
                    self._store.update_status(
                        session_id, SessionStatus.FAILED, error=detail,
                    )
                    self._finalize_run(_run_ctx, None, "failed", error=detail)
            except ValueError:
                logger.warning(
                    "Session %s was deleted before state could be persisted — "
                    "this is expected if the session was concurrently removed.",
                    session_id[:8],
                )
            self._cancellation_tokens.pop(session_key, None)
            self._active_evidence_stores.pop(session_id, None)
            if _evidence_store is not None:
                self._active_evidence_requirements.pop(
                    _evidence_store.root_run_id, None,
                )
            if _evidence_store is not None:
                self._evidence_stores.finish(_evidence_store.root_run_id)

    # ── Run lifecycle helper ───────────────────────────────────────────────

    def _finalize_run(
        self,
        run_ctx: Any,
        result: RunResult | None,
        status: str,
        *,
        error: str = "",
    ) -> None:
        """Atomically transition a Run and append its durable terminal fact."""
        if run_ctx is None:
            return
        _run_id = getattr(run_ctx, "run_id", None)
        if not _run_id:
            return

        try:
            _summary = result.summary if result is not None else ""
            _steps = result.steps_taken if result is not None else 0
            _tokens = result.total_tokens if result is not None else 0
            _termination_reason = (
                result.termination_reason.value
                if result is not None and hasattr(result.termination_reason, "value")
                else str(getattr(result, "termination_reason", "") or "none")
            )
            _verification_status = (
                result.verification_status.value
                if result is not None and hasattr(result.verification_status, "value")
                else str(getattr(result, "verification_status", "") or "not_applicable")
            )
            _verification_reason = (
                result.verification_reason.value
                if result is not None and hasattr(result.verification_reason, "value")
                else str(getattr(result, "verification_reason", "") or "none")
            )
            _checks = [
                asdict(check) if is_dataclass(check) else dict(check)
                for check in (getattr(result, "verification_checks", ()) or ())
            ] if result is not None else []
            _delta_obj = getattr(result, "workspace_delta", None)
            _workspace_delta = (
                asdict(_delta_obj)
                if is_dataclass(_delta_obj)
                else dict(_delta_obj or {})
            )
            _session_id = getattr(run_ctx, "session_id", "")
            _evidence_manager = getattr(self, "_evidence_stores", None)
            _requirements_by_run = getattr(
                self, "_active_evidence_requirements", {},
            )
            _terminal_store = (
                _evidence_manager.get(_run_id)
                if _evidence_manager is not None else None
            )
            _requirements = _requirements_by_run.get(_run_id)
            if _terminal_store is not None and _requirements is not None:
                from agent.session.run_evidence import (
                    EvidenceEntry,
                    EvidenceKind,
                    EvidenceStatus,
                )
                _evaluation = (
                    _terminal_store.last_evaluation
                    or _terminal_store.evaluate(_requirements)
                )
                _terminal_store.record(EvidenceEntry(
                    evidence_id=f"ev_completion_{_run_id}",
                    idempotency_key=f"completion-evaluated:{_run_id}",
                    root_run_id=_run_id,
                    session_id=_session_id,
                    producer_session_id=_session_id,
                    kind=EvidenceKind.COMPLETION_EVALUATED,
                    status=(
                        EvidenceStatus.SUCCEEDED
                        if _evaluation.satisfied else EvidenceStatus.FAILED
                    ),
                    summary=(
                        "requirements satisfied"
                        if _evaluation.satisfied
                        else "completion requirements not satisfied"
                    ),
                    depends_on=_evaluation.satisfied_evidence_ids,
                    metadata={
                        "satisfied": _evaluation.satisfied,
                        "total_required": _evaluation.total_required,
                        "total_satisfied": _evaluation.total_satisfied,
                        "satisfied_by": {
                            key: list(value)
                            for key, value
                            in _evaluation.satisfied_by.items()
                        },
                        "missing": [asdict(item) for item in _evaluation.missing],
                        "failed": [asdict(item) for item in _evaluation.failed],
                    },
                ))

            # 1. CAS update — best-effort transition from 'running'
            _updated = self._store.finalize_run_with_event(
                _run_id,
                _session_id,
                status=status,
                summary=_summary,
                steps_taken=_steps,
                total_tokens=_tokens,
                error=error,
                event_payload={
                    "turn_id": getattr(run_ctx, "turn_id", ""),
                    "turn_index": getattr(run_ctx, "turn_index", 0),
                    "termination_reason": _termination_reason,
                    "verification_status": _verification_status,
                    "verification_reason": _verification_reason,
                    "verification": {
                        "status": _verification_status,
                        "reason": _verification_reason,
                        "checks": _checks,
                    },
                    "workspace_delta": ({
                        key: value for key, value in _workspace_delta.items()
                        if key != "patch"
                    } | {
                        "patch_available": bool(_workspace_delta.get("patch")),
                    }) if _workspace_delta else {},
                    "evidence_summary": _build_terminal_evidence_summary(
                        _terminal_store,
                    ),
                },
                expect_status="running",
            )
            if not _updated:
                logger.warning("Run %s CAS running→%s failed (already terminal) — "
                              "run_terminal will still be emitted",
                              _run_id[:8], status)

            # 2. Broadcast run_terminal — ALWAYS, even if CAS failed.
            #    If we don't emit this, the frontend stays in "Running" forever.
        except Exception:
            logger.exception("Failed to finalize run %s", _run_id)

    # ── Child subagent ──
    # These methods delegate to the implementations in runtime_spawn.py.
    # This avoids a monkey-patch at module bottom and ensures edits to
    # the real logic go to the right file.
    # Run 'from agent.session import runtime_spawn' to see the actual code.

    def spawn_agent(self, **kwargs: Any) -> Any:
        """Delegate to the canonical implementation in runtime_spawn.py."""
        from agent.session.runtime_spawn import spawn_agent as _impl
        return _impl(self, **kwargs)

    def _execute_child_session(self, **kwargs: Any) -> Any:
        """Delegate to the canonical implementation in runtime_spawn.py."""
        from agent.session.runtime_spawn import _execute_child_session as _impl
        return _impl(self, **kwargs)

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
            except BaseException as exc:
                # Re-raise SystemExit and KeyboardInterrupt — they are
                # process-level signals, not subagent failures.
                if isinstance(exc, (SystemExit, KeyboardInterrupt)):
                    raise
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
        """Queue bounded steering for a live child or resume a terminal child."""
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
                outcome=AgentMessageOutcome.LIVE_MESSAGE_QUEUED,
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
        _resume_evidence_store = self._active_evidence_stores.get(parent.id)
        if _resume_evidence_store is not None:
            _resume_evidence_store.retain()

        def execute():
            try:
                return self._execute_child_session(
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
                    evidence_store=_resume_evidence_store,
                )
            finally:
                if _resume_evidence_store is not None:
                    _resume_evidence_store.release()
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
            raise ValueError(f"Unknown session: {child.id}")
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
        execution_repo_path: str | None = None,
        child_metadata: dict[str, object] | None = None,
        child_created_callback=None,
    ) -> AgentRunResult:
        """Compatibility entrypoint for a fresh named child."""
        return self.spawn_agent(
            parent_session_id=parent_session_id,
            request=AgentSpawnRequest.named(
                definition=definition,
                description=description,
                prompt=prompt,
                execution_placement=ExecutionPlacement.FOREGROUND,
            ),
            budget_tokens=budget_tokens,
            parent_max_steps=parent_max_steps,
            cancellation_token=cancellation_token,
            parent_policy=parent_policy,
            origin=origin,
            spawn_context=spawn_context,
            execution_repo_path=execution_repo_path,
            child_metadata=child_metadata,
            child_created_callback=child_created_callback,
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
                "Subagent event callback failed for %s",
                child_session_id, exc_info=True,
            )

    def _build_registry_for_session(
        self, spec: AgentDefinition, session,
        *,
        mode_policy=None,
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
            mode_policy=mode_policy,
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
            self._tool_availability_guard.register(name)

        # Check for failed MCP servers
        failed_servers = getattr(self._mcp_integration, "failed_servers", None)
        if failed_servers:
            for server_name, reason in failed_servers.items():
                server_tools = getattr(self._mcp_integration, "server_tools", {}).get(server_name, [])
                for tool_name in server_tools:
                    self._tool_availability_guard.mark_unavailable(
                        tool_name, f"MCP server '{server_name}': {reason}",
                    )

    def resolve_child_permission_mode(
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

    def ensure_approval_broker(self, session_id: str) -> "ApprovalBroker":
        """Get or create the per-session ApprovalBroker. (P2: public API)

        One broker per session.  The agent thread blocks on
        ``broker.wait_for_decision()``; the HTTP handler resolves via
        ``broker.resolve()``.  This is the exact same synchronous-blocking
        pattern as CC's stdin ``control_response``.
        """
        with self._active_sessions_lock:
            if session_id not in self._approval_brokers:
                from server.services.approval_broker import ApprovalBroker
                self._approval_brokers[session_id] = ApprovalBroker(session_id)
            return self._approval_brokers[session_id]

    def get_approval_broker(self, session_id: str) -> "ApprovalBroker | None":
        """Return the ApprovalBroker for *session_id*, if one exists."""
        with self._active_sessions_lock:
            return self._approval_brokers.get(session_id)

    # ── R4: IoC ports — replace direct _ field writes from server layer ──

    def set_web_mode(self, enabled: bool) -> None:
        self._is_web_mode = enabled

    def set_stats_recorder(self, recorder: object | None) -> None:
        self._stats_recorder = recorder

    def set_evidence_event_callback(self, callback: object | None) -> None:
        if hasattr(self, "_evidence_stores") and self._evidence_stores is not None:
            self._evidence_stores.set_event_callback(callback)

    def set_memory_event_callback(self, callback: object | None) -> None:
        self._memory_event_callback = callback

    def update_run(self, run_id: str, **kwargs) -> object:
        """Public accessor for SessionStore.update_run()."""
        return self._store.update_run(run_id, **kwargs)

    def set_web_confirm_callback(
        self, session_id: str, callback: "WebConfirmCallback",
    ) -> None:
        """Register a web_confirm_callback for the next run of *session_id*.

        Called by agent_service before run_session().  The callback is
        injected into the PermissionPipeline during registry construction.
        """
        self._web_confirm_callbacks[session_id] = callback

    def set_stream_callback(
        self, session_id: str, callback: "StreamCallback",
    ) -> None:
        """Register a real-time thought streaming callback for *session_id*.

        Called by agent_service before run_session().  During LLM generation,
        each text delta is forwarded to this callback so the frontend can
        render thoughts as they arrive instead of waiting for completion.
        """
        self._stream_callbacks[session_id] = callback

    def set_text_stream_callbacks(
        self,
        session_id: str,
        lifecycle_callback: object,
        delta_callback: object,
    ) -> None:
        """Register assistant text streaming callbacks.

        Called by ChatPipeline before run_session().  During LLM generation:
          - lifecycle_callback("start"|"end"|"aborted", block_id, reason)
          - delta_callback(block_id, text)
        """
        self._text_lifecycle_callbacks[session_id] = lifecycle_callback
        self._text_delta_callbacks[session_id] = delta_callback

    # ── Model switching (mid-session) ────────────────────────────────────

    def set_pending_model(self, session_id: str, model: str, provider: str = "") -> None:
        """Queue a model switch for the next run of *session_id*.

        The switch takes effect on the next call to run_session() —
        the backend is rebuilt before the agent starts.
        """
        if not hasattr(self, '_pending_model_switches'):
            self._pending_model_switches: dict[str, tuple[str, str]] = {}
        self._pending_model_switches[session_id] = (model, provider)

    def pop_pending_model(self, session_id: str) -> tuple[str, str] | None:
        """Pop and return a queued model switch, or None."""
        return getattr(self, '_pending_model_switches', {}).pop(session_id, None)

    def set_pending_effort(self, session_id: str, effort: str) -> None:
        if not hasattr(self, '_pending_effort'):
            self._pending_effort: dict[str, str] = {}
        self._pending_effort[session_id] = effort

    def pop_pending_effort(self, session_id: str) -> str | None:
        return getattr(self, '_pending_effort', {}).pop(session_id, None)

    def _publish_evidence_event(self, entry: Any, session_id: str) -> None:
        """Publish an EvidenceEntry as a WS event through the EventBus."""
        if self._event_callback is None:
            return
        try:
            from agent.task import Event, EventType
            self._event_callback(Event(
                event_type=EventType("evidence_record"),
                task_id=session_id,
                session_id=session_id,
                payload={"evidence": entry.to_dict()},
            ))
        except Exception:
            pass

    def record_skill_activation(
        self, skill_name: str, *, source: str = "", fingerprint: str = "",
        mcp_dependencies: list[str] | None = None, session_id: str = "",
    ) -> None:
        """Record a Skill activation in the owning run or its session queue."""
        if not session_id:
            raise ValueError("session_id is required for Skill evidence")
        activation = {
            "skill_name": skill_name,
            "source": source,
            "fingerprint": fingerprint,
            "mcp_dependencies": mcp_dependencies or [],
            "session_id": session_id,
        }
        active_store = self._active_evidence_stores.get(session_id)
        if active_store is not None:
            _flush_skill_activations(active_store, [activation])
            return
        self._pending_skill_activations.setdefault(session_id, []).append(
            activation,
        )

    def set_pending_skill_modifier(self, session_id: str, modifier: Any) -> None:
        if not hasattr(self, "_pending_skill_modifiers"):
            self._pending_skill_modifiers: dict[str, Any] = {}
        self._pending_skill_modifiers[session_id] = modifier

    def pop_pending_skill_modifier(self, session_id: str) -> Any:
        return getattr(self, "_pending_skill_modifiers", {}).pop(
            session_id,
            None,
        )

    def set_pending_thinking(self, session_id: str, enabled: bool) -> None:
        if not hasattr(self, '_pending_thinking'):
            self._pending_thinking: dict[str, bool] = {}
        self._pending_thinking[session_id] = enabled

    def pop_pending_thinking(self, session_id: str) -> bool | None:
        return getattr(self, '_pending_thinking', {}).pop(session_id, None)

    def set_pending_permission_mode_override(self, session_id: str, mode: str) -> None:
        self.set_permission_mode_for_session(session_id, mode)

    def _mcp_tool_names_for_spec(self, spec: AgentDefinition) -> frozenset[str]:
        if self._mcp_integration is None:
            return frozenset()
        from agent.tool_availability_guard import ToolAvailabilityState
        server_tools = self._mcp_integration.server_tools
        raw_names: set[str] = set()

        # CC-aligned: resolve named mcpServers references from frontmatter
        if spec.mcp_servers:
            for entry in spec.mcp_servers:
                if isinstance(entry, str):
                    raw_names.update(server_tools.get(entry, []))
                elif isinstance(entry, dict):
                    # Inline definition — connected at agent start, tools lazy-registered
                    for name in entry:
                        raw_names.update(server_tools.get(name, []))
        # Fallback (backward compat): EDIT-intent agents get session-level MCP tools
        elif spec.intent is TaskIntent.EDIT:
            raw_names.update(
                getattr(self._mcp_integration, "tool_names", frozenset()),
            )

        # Skill dependencies remain deferred until invocation, but their tools
        # must already exist in the session registry so activation can expose
        # the schemas on the following model turn.  This also enables read-only
        # Plan/Research agents to use explicitly declared Skill MCP dependencies
        # without granting every unrelated global MCP server.
        skill_registry = self._base_registry.skill_registry
        if skill_registry is not None:
            dependency_servers = {
                server_name
                for metadata in skill_registry.list_skills()
                for server_name in getattr(
                    metadata,
                    "mcp_servers",
                    frozenset(),
                )
            }
            for server_name in dependency_servers:
                raw_names.update(server_tools.get(server_name, ()))

        return frozenset(
            n
            for n in raw_names
            if self._tool_availability_guard.state_for(n) is ToolAvailabilityState.AVAILABLE
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

    def _build_runtime_messages(
        self, spec: AgentDefinition, task_description: str, *,
        session_id: str = "",
    ) -> list[LLMMessage]:
        """委托给 runtime_prompt_builder。"""
        from agent.session.runtime_prompt_builder import build_runtime_messages
        skill_registry = self._base_registry.skill_registry
        _on_preload = (
            lambda skill_name, source, fingerprint, mcp_dependencies:
                self.record_skill_activation(
                    skill_name, source=source, fingerprint=fingerprint,
                    mcp_dependencies=mcp_dependencies, session_id=session_id,
                )
        ) if session_id else None
        return build_runtime_messages(
            spec, task_description,
            agent_registry=self._agent_registry,
            project_dir=self._agent_registry.project_dir if self._agent_registry else None,
            skill_registry=skill_registry,
            on_skill_preload=_on_preload,
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


# ── Product mode derivation (legacy fallback) ─────────────────────────────


def _derive_product_mode(agent_name: str) -> str:
    """Derive product_mode from agent_name for backward compatibility."""
    _map = {
        "plan": "plan",
        "build": "build",
        "orchestrator": "multi-agent",
    }
    return _map.get(agent_name, "build")


def _build_terminal_evidence_summary(store) -> dict[str, object]:
    if store is None:
        return {"total": 0, "by_kind": {}, "failed": 0}
    entries = store.snapshot()
    by_kind: dict[str, int] = {}
    failed = 0
    for entry in entries:
        by_kind[entry.kind.value] = by_kind.get(entry.kind.value, 0) + 1
        if entry.status.value in {"failed", "blocked", "cancelled"}:
            failed += 1
    return {
        "total": len(entries),
        "by_kind": by_kind,
        "failed": failed,
    }


def _flush_skill_activations(store, activations: list[dict[str, object]]) -> None:
    """Record pending skill activations as SKILL_LOADED evidence."""
    if not activations:
        return
    from agent.session.run_evidence import (
        EvidenceEntry, EvidenceKind, EvidenceStatus,
    )
    for activation in activations:
        skill_name = str(activation.get("skill_name", ""))
        fingerprint = str(activation.get("fingerprint", ""))
        source = str(activation.get("source", ""))
        session_id = str(activation.get("session_id", ""))
        store.record(EvidenceEntry(
            evidence_id=f"ev_skill_{skill_name}_{source}"[:40],
            idempotency_key=f"skill-load:{session_id}:{skill_name}:{fingerprint}:{source}",
            root_run_id="",
            session_id=session_id,
            producer_session_id=session_id,
            kind=EvidenceKind.SKILL_LOADED,
            status=EvidenceStatus.SUCCEEDED,
            tool_name=f"skill:{skill_name}",
            source_fingerprint=fingerprint,
            metadata={"source": source, "mcp_dependencies": list(activation.get("mcp_dependencies", []))},
        ))
