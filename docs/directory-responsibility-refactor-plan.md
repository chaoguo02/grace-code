# Directory Responsibility Refactor Plan

## 1. Purpose

This document turns the current architecture analysis into a directory-level
responsibility map for `forge-agent`.

It does **not** propose immediate code changes. Its purpose is to:

- clarify ownership for each top-level part;
- identify where responsibilities have drifted back into central objects;
- define what should stay, what should move, and what should stop growing;
- provide a stable basis for task assignment before refactoring begins.

The central problem is not that the project lacks folders or subsystem intent.
The project already has strong subsystem boundaries in naming and documentation.
The actual problem is that control has flowed back into a few oversized centers,
especially:

- `agent/core.py`
- `entry/cli.py`

As a result, the codebase is physically partitioned but still logically
centralized.

## 2. Current Diagnosis

### 2.1 What is already good

The project already shows clear architectural intent:

- `agent/` aims to own execution behavior.
- `context/` aims to own context lifecycle and budgeting.
- `memory/` aims to own persistent knowledge, retrieval, and consolidation.
- `tools/` aims to own capability units.
- `hitl/` and `hooks/` aim to own cross-cutting control and extensibility.
- `llm/` aims to own model/provider adaptation.
- `observability/` aims to own passive tracing and validation.
- `agent/v2/` aims to own session runtime and task delegation.

This means the project does **not** need a conceptual rewrite. It needs
responsibility enforcement.

### 2.2 What is currently wrong

The major issue is responsibility re-centralization:

- `agent/core.py` owns too many decisions from too many domains.
- `entry/cli.py` acts as an application assembler, system bootstrapper, and
  mode controller all at once.
- several lower-level parts still know too much about higher-level parts;
- some concepts exist twice with overlapping names or roles.

In practice, this means:

- directory boundaries exist on disk;
- ownership boundaries are not yet enforced in code.

## 3. Architectural Principle

The guiding principle for the refactor should be:

> Each directory should own one kind of responsibility, expose a small surface,
> and stop pulling unrelated policy back into itself.

The practical form of that principle is:

- `agent` runs;
- `context` assembles;
- `memory` stores and retrieves;
- `tools` execute capabilities;
- `runtime` supports low-level execution infrastructure;
- `entry` accepts user input and bootstraps the app;
- `hitl` and `hooks` enforce cross-cutting controls;
- `llm` adapts providers;
- `observability` records what happened.

## 4. Directory Responsibility Map

## 4.1 `agent/`

### Intended role

`agent/` should own execution orchestration.

This means:

- the main task loop;
- step progression;
- transition between think / act / observe / finish;
- coordination of collaborators;
- high-level run lifecycle.

### What it should keep

- main execution loop coordination;
- finish / give-up handling;
- per-step runtime coordination;
- task-level control flow;
- agent-facing abstractions and policies.

### What it should not keep

- full context assembly logic;
- memory injection strategy;
- artifact storage rules;
- session persistence details;
- observability composition details;
- direct hook/pipeline orchestration logic;
- low-level LLM retry/backoff details;
- multi-subsystem recovery policy embedded inline in the loop.

### Current problem

`agent/core.py` currently mixes:

- policy handling;
- context assembly;
- memory interaction;
- observation shaping;
- runtime recovery;
- tracing/observability;
- budget and loop protections;
- completion validation.

That makes `ReActAgent` both coordinator and subsystem host.

### Required refactor direction

`agent/` should shrink toward a coordinator model.

Expected collaborator extraction targets:

- `LLMInvoker`
- `ToolObservationCoordinator`
- `RunFinalizer`
- `ExecutionLoopState`
- runtime guard collaborators already hinted at by `runtime_controller`

### Acceptance criteria

- `agent/core.py` no longer directly owns most context/memory assembly logic;
- `ReActAgent.run()` reads primarily like orchestration;
- subsystem behavior is delegated to named collaborators.

## 4.2 `context/`

### Intended role

`context/` should own request context lifecycle.

This includes:

- request assembly;
- context-layer budgeting;
- session/task context modeling;
- compaction policy;
- artifact summary inclusion;
- context tracing and measurement.

### What it should keep

- `ContextManager`
- token budgeting
- history shaping
- task/session summary structures
- context stats and traces
- compaction policy and transforms
- artifact references as prompt inputs

### What it should not keep

- tool execution;
- memory storage implementation;
- CLI/session command handling;
- agent control flow;
- provider-specific LLM details.

### Current problem

`context/` already has strong design intent, but it does not yet fully own
request assembly. The project still has important assembly decisions embedded in
`agent/core.py`, and chat/session retention behavior is still partially managed
outside the context subsystem.

### Required refactor direction

`context/` should become the single owner of request assembly.

The intended subsystem should fully own:

- `SessionState`
- `TaskContext`
- `TaskSummary`
- `ContextStats`
- `ContextTrace`
- routing between current-task fidelity and cross-task summary retention

### Acceptance criteria

- all request message assembly flows through `ContextManager`;
- unrelated rounds do not replay raw prior tool output;
- compaction changes session state consistently, not only one prompt build;
- budget errors can be explained with a context-layer breakdown.

## 4.3 `memory/`

### Intended role

`memory/` should own durable knowledge storage, retrieval, freshness, and
consolidation.

### What it should keep

- memory store implementations;
- metadata cache;
- retrieval/ranking logic;
- consolidation and extraction flows;
- memory modeling and typing;
- proactive memory generation;
- long-term knowledge maintenance.

### What it should not keep

- knowledge of concrete agent runtime classes;
- direct participation in the main execution loop;
- responsibility for prompt assembly;
- direct CLI lifecycle decisions.

### Current problem

The subsystem is feature-rich, but some boundaries are inverted. Memory-side
code should not need to know about specific agent runtime implementations.

### Required refactor direction

Separate `memory/` into clearly enforced layers:

- storage;
- retrieval/ranking;
- candidate generation/consolidation;
- injection interface consumed by `context/`.

The memory subsystem should return data and candidates, not actively steer the
main loop.

### Acceptance criteria

- `memory/` does not depend on concrete agent runtime implementations;
- memory writes and retrievals are triggered through explicit interfaces;
- prompt injection policy lives in `context/`, not `memory/`.

## 4.4 `tools/`

### Intended role

`tools/` should own executable capability units.

### What it should keep

- tool definitions;
- schemas;
- execution contracts;
- tool-specific risk classification;
- tool results;
- tool-local helper abstractions.

### What it should not keep

- session orchestration;
- high-level retry strategies unrelated to a single tool;
- application assembly concerns;
- cross-system policy composition.

### Current problem

This layer is healthier than most, but there is conceptual overlap between:

- tool definitions and execution registries in `tools/`;
- similarly named runtime abstractions elsewhere.

### Required refactor direction

Keep `tools/` as the source of capability definitions and results, while making
sure outer controls remain outside individual tools:

- permissions in `hitl/`;
- hooks in `hooks/`;
- capability availability in agent/runtime support layers.

### Acceptance criteria

- new tools can be added without editing central loop logic;
- tool abstractions remain focused on capability behavior;
- registry naming no longer causes confusion across layers.

## 4.5 `runtime/`

### Intended role

`runtime/` should own low-level execution support, not application business
ownership.

Examples include:

- streaming/tool execution helpers;
- MCP bridge support;
- goal-related execution support;
- generalized runtime primitives.

### What it should keep

- low-level runtime helpers;
- MCP transport/adapter/config support;
- generalized query/streaming execution primitives;
- low-level execution contracts.

### What it should not keep

- a second competing top-level application runtime story;
- ambiguous ownership relative to `agent/v2/`;
- user-facing orchestration logic.

### Current problem

The term "runtime" currently means more than one thing in the project:

- command execution runtime in `tools/runtime.py`;
- tool/runtime abstractions in `runtime/`;
- session/task runtime in `agent/v2/`.

This is not automatically wrong, but it is currently too ambiguous.

### Required refactor direction

The team should explicitly choose one of these models:

1. `runtime/` is the low-level engine layer and `agent/v2/` is a consumer.
2. `runtime/` is a parallel experimental stack and must be narrowed.

Without this decision, conceptual duplication will continue to grow.

### Acceptance criteria

- the role of `runtime/` can be stated in one sentence;
- registry/runtime terminology no longer overlaps confusingly with `tools/` and
  `agent/v2/`;
- low-level runtime code is clearly below orchestration layers.

## 4.6 `agent/v2/`

### Intended role

`agent/v2/` should own session-oriented orchestration and child-task delegation.

This is currently the closest subsystem to the intended Claude Code-style model.

### What it should keep

- session runtime orchestration;
- parent/child session semantics;
- session store contract;
- task delegation model;
- compact child-result return boundary;
- agent definition and visibility rules.

### What it should not keep

- direct ownership of worktree lifecycle details;
- mixed responsibility for execution environment mechanics;
- low-level task ledger persistence details inside orchestration code;
- too many runtime support policies embedded directly in `SessionRuntime`.

### Current problem

`SessionRuntime` and `subagent.py` are both accumulating multiple unrelated
responsibilities:

- session orchestration;
- registry building;
- idempotency/task ledger;
- capability/circuit management;
- child dispatch;
- worktree creation/merge/cleanup.

### Required refactor direction

Expected target collaborators:

- `SessionOrchestrator`
- `SessionRegistryBuilder`
- `SubagentDispatcher`
- `RepoScopeResolver`
- `TaskLedgerService`
- worktree lifecycle support separated from subagent execution logic

### High-priority risk

Child execution path correctness around repository scope must be stabilized first
before deeper structural refactors continue.

### Acceptance criteria

- `SessionRuntime` becomes an orchestrator, not a subsystem container;
- child execution always inherits the correct repo scope;
- worktree strategy becomes replaceable without rewriting subagent flow.

## 4.7 `entry/`

### Intended role

`entry/` should own user-facing startup and mode dispatch.

### What it should keep

- command parsing;
- CLI/chat entrypoints;
- user-facing mode routing;
- invocation of application builders and services;
- renderer/session wiring at a high level.

### What it should not keep

- tool registry assembly details;
- memory system assembly details;
- hook system assembly details;
- permission pipeline assembly details;
- deep runtime construction logic;
- too much session lifecycle logic.

### Current problem

`entry/cli.py` is currently a large application assembler. It initializes
memory, hooks, permissions, tools, renderers, runtimes, validation flows, and
multiple modes. `entry/chat.py` also owns more session and subsystem logic than a
thin entry layer should.

### Required refactor direction

Expected extraction targets:

- `ApplicationBuilder`
- `RegistryFactory`
- `ModeRunner`
- `SessionBootstrap`
- chat-specific orchestration services outside the raw entrypoint

### Acceptance criteria

- `entry/cli.py` becomes mostly command parsing plus handoff;
- subsystem bootstrap logic is delegated to named builders/factories;
- the entry layer stops being a hidden architecture center.

## 4.8 `hitl/`

### Intended role

`hitl/` should own human-in-the-loop approval and permission policy evaluation.

### What it should keep

- permission rules;
- permission evaluation pipeline;
- approval request/response models;
- rule persistence and inference helpers.

### What it should not keep

- direct coupling to main-loop logic beyond its approval surface;
- business behavior outside permission decisions.

### Current assessment

This subsystem is directionally healthy. The main improvement required is not a
major redesign, but consistency of integration.

### Acceptance criteria

- all permission decisions flow through the same pipeline surface;
- legacy approval paths are clearly marked as compatibility-only if retained.

## 4.9 `hooks/`

### Intended role

`hooks/` should own event-driven extensibility.

### What it should keep

- event definitions;
- matcher logic;
- internal/external hook registry;
- dispatcher/executor behavior.

### What it should not keep

- application-specific policy logic;
- ad hoc integration rules scattered outside the dispatcher path.

### Current assessment

The subsystem is well-shaped. The main need is to stop parallel ad hoc
integration styles from reappearing in other layers.

### Acceptance criteria

- pre/post event extension points are routed consistently through dispatcher
  abstractions;
- no duplicate extension mechanisms emerge elsewhere.

## 4.10 `llm/`

### Intended role

`llm/` should own provider adaptation and normalized model I/O.

### What it should keep

- backend implementations;
- message/tool schema normalization;
- streaming support;
- provider selection/router logic.

### What it should not keep

- strong dependence on upper-layer orchestration semantics when avoidable;
- session/task behavior logic.

### Required refactor direction

Where possible, use neutral shared model structures rather than upper-layer
execution semantics leaking downward into adapters.

### Acceptance criteria

- `llm/` reads like a provider adapter layer;
- higher-order agent/runtime policy is not embedded in backend classes.

## 4.11 `observability/`

### Intended role

`observability/` should own passive recording, scoring, validation, and
traceability.

### What it should keep

- tracing;
- dataset emission;
- validation helpers;
- scoring;
- filtering/reporting utilities.

### What it should not keep

- main business flow ownership;
- control decisions that belong to runtime/agent/context layers.

### Current assessment

The subsystem is structurally sound and should remain passive.

### Acceptance criteria

- observability components record and summarize behavior;
- they do not become hidden drivers of execution behavior.

## 5. Cross-Cutting Structural Problems

These are not confined to a single directory and must be treated explicitly.

### 5.1 Central-object gravity

The largest architectural force in the codebase is the tendency for new logic to
flow back into:

- `agent/core.py`
- `entry/cli.py`

This is the main reason directory boundaries have not yet become ownership
boundaries.

### 5.2 Responsibility inversion

Some lower layers still know too much about upper layers. This causes future
refactors to become expensive because supposedly reusable subsystems are not
actually independent.

### 5.3 Duplicate concepts

Several concepts exist in more than one form:

- runtime
- registry
- session lifecycle
- permission path compatibility layers

This is survivable in a growing project, but only if the team now chooses a
canonical ownership model.

### 5.4 Physical partition without behavioral partition

The project is already split by folders, but the behavior is not fully split by
owner. That is the core architectural mismatch to resolve.

## 6. Priority-Ordered Operations

The accumulated work should be handled in this order.

### Phase 1: Establish responsibility rules

Before refactoring code, define for each directory:

- owned responsibilities;
- forbidden responsibilities;
- allowed dependencies;
- forbidden dependencies.

This acts as an architectural constitution and prevents new drift during the
refactor.

### Phase 2: Stabilize the baseline

Before structural work:

- make tests reproducible in local and CI environments;
- isolate online/network-dependent E2E checks from the stable offline default;
- fix the highest-risk runtime-path correctness issues first.

This reduces the chance of mixing architectural refactor work with avoidable
environmental noise.

### Phase 3: Freeze behavioral invariants

Add or tighten tests around:

- main loop behavior;
- tool observation roundtrip;
- parent/child delegation boundary;
- permission order;
- context retention/compaction behavior.

This provides guardrails so the refactor preserves behavior.

### Phase 4: Shrink `agent/core.py`

This is the highest-value structural change and should begin before broad
directory cleanups elsewhere.

If this is not done first, responsibilities removed from one subsystem are likely
to continue flowing back into the core loop.

### Phase 5: Shrink `entry/cli.py`

Once orchestration has started moving out of the main loop, stop the entry layer
from remaining the second architecture center.

### Phase 6: Refactor `agent/v2/` runtime boundaries

After the main centers are reduced:

- separate session orchestration from repo/worktree strategy;
- separate child dispatch from task ledger and capability support policies.

### Phase 7: Resolve duplicate abstractions

After boundaries are clearer, resolve or rename overlapping abstractions such as:

- registry concepts;
- runtime concepts;
- legacy/new permission path ownership.

### Phase 8: Resume feature growth on top of cleaner ownership

Only after the ownership model is enforceable should major new feature expansion
continue.

## 7. Suggested Team Partitioning

The refactor is suitable for parallel ownership if boundaries are agreed first.

Suggested lanes:

- Stability and test baseline
- `agent/` loop slimming
- `context/` ownership expansion
- `memory/` boundary cleanup
- `agent/v2/` orchestration split
- `entry/` builder/bootstrap extraction
- `runtime/tools/hitl/hooks` abstraction cleanup

Each lane should be measured by whether it reduces ownership ambiguity, not only
by line movement.

## 8. Success Criteria

The refactor should be considered successful when all of the following are true:

- each top-level directory has a clear, enforceable purpose;
- the main loop coordinates rather than hosts subsystem behavior;
- entrypoints bootstrap and dispatch rather than assemble everything inline;
- session/runtime orchestration is separated from environment mechanics;
- context policy is owned by `context/`;
- memory becomes a true service layer, not a semi-orchestrator;
- adding a new capability does not require editing central god objects;
- the project can continue growing without every new concern flowing back into
  `core.py` or `cli.py`.

## 9. Final Summary

The project is already architecturally ambitious in a good way. The problem is
not lack of subsystem intent. The problem is that subsystem ownership is not yet
fully enforced in the implementation.

The next stage should therefore not be "add more layers" or "rename folders". It
should be:

1. declare ownership;
2. stop responsibility drift;
3. shrink the central objects;
4. let each directory become the real owner of its own domain.

That path preserves current logic while making future work safer, more parallel,
and more maintainable.
