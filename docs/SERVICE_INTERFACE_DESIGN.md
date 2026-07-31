# Phase 4b: Service Interface Design

## Purpose

This document defines the interface contracts for extracting four
misplaced service concerns from `SessionRuntime`.  It is the
**required gate** before any code extraction begins — per the
CONTEXT_MODULE_REFACTORING_DESIGN.md acceptance criteria.

Each service below must have its interface design approved before
the first line of extraction code is written.  Extraction is done
one service at a time, with a full test suite pass after each.

## 1. Service Inventory

| Service | Lines in runtime.py | Complexity | Risk |
|---------|---------------------|------------|------|
| A. AgentTeamService | ~500 lines (10 methods) | High – multi-agent coordination with in-memory state | Medium – pure team lifecycle, no Runtime DB access |
| B. WorktreeResolutionService | ~80 lines + daemon thread | Medium – async queue + worker | High – TOCTOU race on result dict, worker never joined |
| C. HeadlessApprovalService | ~50 lines | Low – per-session broker map | Low – transport-layer concern |
| D. RunLifecycleService | ~160 lines | Medium – CAS + WS broadcast | Medium – depends on _store + _publish_run_terminal + evidence_stores |

## 2. Extraction Order (Recommended)

**2a: HeadlessApprovalService** (lowest risk, smallest scope). Pilot.
**2b: RunLifecycleService** (medium risk, well-defined input/output).
**2c: WorktreeResolutionService** (needs TOCTOU fix during extraction).
**2d: AgentTeamService** (deferred — requires team state durability design).

## 3. Service A: AgentTeamService

### 3.1 Current Location

`SessionRuntime` methods (lines 390-910 approximately):
- `propose_agent_team()`
- `approve_agent_team()`
- `reject_agent_team()`
- `coordinate_agent_team()`
- `claim_team_task()`
- `complete_team_task()`
- `execute_team_task()`
- `resolve_team_task_review()`
- `send_team_message()`
- `shutdown_agent_team()`
- Plus instance state: `self._teams: dict[str, TeamRuntime]`, `self._team_proposals: dict[str, dict]`

### 3.2 `self.*` Dependencies Mapped

Each team method accesses these Runtime attributes:

| `self.*` access | Type | Can be parameterized? |
|----------------|------|----------------------|
| `self._teams` | dict (team state) | YES — becomes service-owned state |
| `self._team_proposals` | dict (pending proposals) | YES — becomes service-owned state |
| `self._store` (SessionStore) | DB access | YES — inject at construction |
| `self._active_sessions_lock` | Lock | NO — service needs its own lock |
| `self._cancellation_tokens` | dict | YES — pass as parameter when cancelling |
| `self._shared_executor` | ThreadPoolExecutor | YES — inject at construction |
| `self.run_session()` | method call | YES — pass as callback |
| `self._spawn_lock` | Lock | NO — service needs its own |

### 3.3 Interface Definition

```python
class AgentTeamService:
    """Multi-agent team lifecycle — extracted from SessionRuntime Phase 4b."""

    def __init__(
        self,
        *,
        store: SessionStore,
        executor: ThreadPoolExecutor,
        run_session_callback: Callable[[str, AgentDefinition, ...], None],
    ) -> None:
        self._store = store
        self._executor = executor
        self._run_session = run_session_callback
        self._lock = threading.Lock()
        self._teams: dict[str, TeamRuntime] = {}
        self._proposals: dict[str, dict] = {}

    # All 10 team methods become methods on this class.
    # Each currently accesses self._teams, self._proposals — now local state.
    # Each currently accesses self._store — now injected.
    # Each currently accesses self._cancellation_tokens — now passed as callback parameter.
```

### 3.4 Communication Contract with Runtime

- Runtime provides `run_session_callback` at construction time — allows team
  service to spawn subagent sessions without directly accessing Runtime internals
- Runtime provides `cancellation_callback(team_id)` — allows team shutdown to
  cancel running members
- Team service owns its own lock — no sharing with Runtime's `_active_sessions_lock`

### 3.5 Thread Safety

- `self._lock` protects `_teams` and `_proposals` dicts
- `self._executor` is the Runtime-owned ThreadPoolExecutor — shared but
  thread-safe by construction (concurrent.futures)
- No access to Runtime's `_cancellation_tokens` dict — uses callback instead

### 3.6 Decision: DEFERRED

AgentTeamService extraction is deferred.  The team state is purely in-memory
— a server restart loses all team state.  Before extracting, a durability
design is needed (SQLite table for team state, or explicit "teams are
ephemeral" contract).  Extraction without durability design would create a
false sense of modularity while preserving the real architectural gap.

**Action**: Create a dedicated "Agent Team State Durability" design issue.
Do NOT extract in Phase 4b.

## 4. Service B: WorktreeResolutionService

### 4.1 Current Location

`SessionRuntime` methods (lines 1346-1440 approximately):
- `_ensure_worktree_worker()` — lazy-init daemon thread
- `enqueue_worktree_command()`
- `get_worktree_command_status()`
- `set_worktree_completion_callback()`
- Plus instance state: `self._worktree_queue`, `self._worktree_results`,
  `self._worktree_worker_started`, `self._worktree_completion_callback`

### 4.2 `self.*` Dependencies Mapped

| `self.*` access | Type | Can be parameterized? |
|----------------|------|----------------------|
| `self._worktree_queue` | queue.Queue | YES — becomes service state |
| `self._worktree_results` | dict | YES — becomes service state |
| `self._worktree_worker_started` | bool | YES — becomes service state |
| `self._worktree_completion_callback` | Callable | YES — set via setter |
| (none from Runtime core) | | — only accesses own fields + queue |

### 4.3 Known Defect

**TOCTOU race** on `_worktree_results` dict: worker thread writes results,
`enqueue_worktree_command` caller checks for duplicates and writes `"queued"`.
No lock on the dict.

### 4.4 Interface Definition

```python
class WorktreeResolutionService:
    """Async worktree resolution queue — extracted from SessionRuntime Phase 4b."""

    def __init__(self, *, completion_callback: Callable | None = None) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._results: dict[str, str] = {}  # child_id_action → status
        self._results_lock = threading.Lock()  # Phase 4b: fix TOCTOU race
        self._worker_started = False
        self._worker_thread: threading.Thread | None = None
        self._completion_callback = completion_callback

    def start(self) -> None: ...
    def stop(self, timeout: float = 5.0) -> None:  # Phase 4b: join worker
        ...
    def enqueue(self, child_id: str, action: str, params: dict) -> str: ...
    def get_status(self, child_id: str, action: str) -> str | None: ...
    def set_completion_callback(self, cb: Callable) -> None: ...
```

### 4.5 Communication Contract with Runtime

- Runtime calls `start()` once during agent service initialization
- Runtime calls `stop()` in `dispose()` to join the worker thread
- `completion_callback` set by agent_service — exactly as today, but via
  explicit setter rather than direct attribute assignment
- Queue operations are thread-safe (queue.Queue)
- Result dict is lock-protected (fixes TOCTOU race)

### 4.6 Decision: EXTRACT (Phase 4b)

Scope is well-bounded.  Only accesses its own fields + `queue.Queue`.
The TOCTOU fix (`_results_lock`) is trivial and included in extraction.

## 5. Service C: HeadlessApprovalService

### 5.1 Current Location

`SessionRuntime` methods/fields:
- `self._approval_brokers: dict[str, ApprovalBroker]`
- `_ensure_approval_broker(session_id)`
- `get_approval_broker(session_id)`
- Cleanup in `cleanup_session()`, `dispose()`

### 5.2 `self.*` Dependencies Mapped

| `self.*` access | Type | Can be parameterized? |
|----------------|------|----------------------|
| `self._approval_brokers` | dict | YES — becomes service state |
| `self._active_sessions_lock` | Lock | NO — service needs its own lock |
| (nothing else) | | Pure broker create/lookup |

### 5.3 Interface Definition

```python
class HeadlessApprovalService:
    """Per-session approval broker registry — extracted from SessionRuntime Phase 4b."""

    def __init__(self) -> None:
        self._brokers: dict[str, ApprovalBroker] = {}
        self._lock = threading.Lock()

    def get_or_create(self, session_id: str) -> ApprovalBroker: ...
    def get(self, session_id: str) -> ApprovalBroker | None: ...
    def remove(self, session_id: str) -> None: ...
    def clear(self) -> None: ...
```

### 5.4 Communication Contract with Runtime

- Runtime creates one `HeadlessApprovalService` at init time
- HTTP handlers call `get_or_create(session_id)` — same as today via
  `_ensure_approval_broker`
- `cleanup_session()` calls `remove(session_id)`
- `dispose()` calls `clear()`

### 5.5 Decision: EXTRACT FIRST (pilot)

Lowest risk, smallest scope.  Perfect pilot for Phase 4b.

## 6. Service D: RunLifecycleService

### 6.1 Current Location

- `_finalize_run()` — 160-line method
- `self._publish_run_terminal` — callback set by agent_service

### 6.2 `self.*` Dependencies Mapped

| `self.*` access | Type | Can be parameterized? |
|----------------|------|----------------------|
| `self._store` (SessionStore) | DB access | YES — inject at construction |
| `self._publish_run_terminal` | Callable | YES — inject at construction |
| `self._evidence_stores` (EvidenceStoreManager) | evidence lookup | YES — inject at construction |
| `self._active_evidence_requirements` | dict | YES — pass as parameter |
| `self._active_evidence_stores` | dict | YES — pass as parameter |

### 6.3 Interface Definition

```python
class RunLifecycleService:
    """Run terminal state management — extracted from SessionRuntime Phase 4b."""

    def __init__(
        self,
        *,
        store: SessionStore,
        evidence_stores: EvidenceStoreManager,
        publish_callback: Callable[[str, dict], None] | None = None,
    ) -> None:
        self._store = store
        self._evidence_stores = evidence_stores
        self._publish = publish_callback

    def finalize(
        self,
        run_ctx: Any,
        result: RunResult | None,
        status: str,
        *,
        error: str = "",
        evidence_requirements: Any = None,
    ) -> None:
        """CAS-update Run record and broadcast run_terminal."""
        ...  # extracted body of _finalize_run
```

### 6.4 Decision: EXTRACT (Phase 4b, after pilot)

Well-defined input/output.  Depends on _store + _publish_run_terminal +
evidence_stores — all injectable.  Extract after HeadlessApprovalService
pilot confirms the pattern works.

## 7. FileReadCache Analysis

### 7.1 Current State

`self._read_cache` is a Runtime-level singleton injected into all file tools.
It uses a plain `dict` with no lock.  Concurrent sessions share it via
`self._base_registry`.

### 7.2 Content-Addressed Investigation

FileReadCache keys are file paths.  Each cache entry is the file content
read at a specific time.  Making it content-addressed would require:

```python
key = hashlib.sha256(f"{file_path}:{os.path.getmtime(file_path)}".encode()).hexdigest()
```

This is **feasible** — `os.path.getmtime()` is cheap and already called
during file reads.  The cache becomes immutable-by-construction: same
path + same mtime → same content → same cache key.

### 7.3 Decision: Content-Addressed (NOT lock)

Do NOT add a lock.  Restructure FileReadCache to use content-addressed
keys (sha256 of path + mtime).  This eliminates the shared mutable state
entirely — no lock needed, no race possible.

**Action**: Create `FileReadCache.make_key(file_path)` static method.
Update `get()` and `set()` to use content-addressed keys.  This is a
Phase 4b deliverable.

## 8. Implementation Order

1. **HeadlessApprovalService** — pilot extraction (lowest risk)
2. **RunLifecycleService** — medium risk, well-defined interface
3. **WorktreeResolutionService** — includes TOCTOU fix
4. **FileReadCache content-addressed** — zero concurrency risk
5. **AgentTeamService** — DEFERRED (requires durability design first)

Each step runs full test suite after extraction.  Tests must pass
before the next step begins.
