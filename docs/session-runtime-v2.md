# Session Runtime V2 Design

## 1. Why this document exists

This document defines the first implementation target for a new `v2` agent
runtime modeled after OpenCode's child-session architecture.

The goal is not to retrofit one more executor into the existing
`react / plan / dag / multi-agent` split. The goal is to add a parallel `v2`
path with one unified runtime model:

- one top-level session runtime;
- one ReAct loop model for all agents;
- child sessions as first-class persisted objects;
- one native `task` tool for task delegation;
- agent behavior controlled by agent config, not by separate runtime classes.

This document reflects the decisions already confirmed for v2:

- `v2` is a new path; existing entrypoints continue to work unchanged;
- `plan` remains a read-only primary agent;
- built-in subagents are `explore` and `general`;
- child sessions are persisted in SQLite from day one;
- the `task` tool only accepts `description`, `subagent_type`, and `prompt`;
- child sessions return summary text only;
- dependency management is primarily model-driven, not DAG-driven;
- child sessions do not inherit parent conversation history by default.

## 2. Non-goals for phase 1

Phase 1 deliberately does not attempt to solve every orchestration problem.

Out of scope:

- replacing existing `react`, `plan`, `dag`, or `multi-agent` flows;
- implementing `plan_exit`-style agent switching;
- adding worktree isolation, file locks, or merge orchestration;
- building a full TUI for parent/child session navigation;
- replaying full child event streams back into the parent prompt;
- implementing DAG scheduling or explicit dependency planning;
- supporting custom user-defined agents in the first code patch.

The first milestone is smaller:

> Parent agent calls `task` -> runtime creates a child session -> subagent runs
> its own ReAct loop -> child returns summary text -> parent continues reasoning.

## 3. Design principles

### 3.1 Unified runtime, configurable agents

V2 should not introduce more top-level executor classes. Agent behavior should be
expressed through agent configuration:

- `mode`: `primary`, `subagent`, or `all`;
- prompt template;
- tool/permission policy;
- visibility (`hidden` or visible);
- model override if needed later.

The runtime remains the same whether the active agent is `build`, `plan`,
`explore`, or `general`.

### 3.2 Child sessions are first-class objects

Child sessions are not temporary helper structs. Each child session must:

- have its own persisted row in SQLite;
- have a parent link;
- own its own message history;
- own its own execution lifecycle;
- be queryable and resumable later.

### 3.3 The parent only receives the conclusion

The parent session should receive the child session's final summary text, not the
full intermediate tool history. This keeps the parent prompt clean and aligns
with the target OpenCode-like model.

### 3.4 The model manages most task decomposition

The runtime should support multiple `task` tool calls in one model response and
execute them in parallel. The runtime should not impose a DAG planner in phase 1.
If the model needs sequencing, it can wait for earlier child results and then
issue later `task` calls in a subsequent ReAct turn.

### 3.5 Permission inheritance before specialization

The child session's permission set starts from the parent session's effective
permission profile, then narrows according to the selected subagent type.

Phase 1 keeps this simple:

- parent permissions define the maximum envelope;
- agent type config narrows the tool/actions allowed;
- permission outcomes remain conceptually `allow / ask / deny`.

## 4. Target architecture

## 4.1 Main components

Phase 1 introduces a new v2 stack:

- `SessionRuntime`
- `SessionStore` backed by SQLite
- `SessionRecord` and `SessionMessage` persistence models
- `AgentRegistryV2`
- `TaskToolV2`
- `AgentRunnerV2`

The current stack remains intact:

- `ReActAgent`
- `PlanExecuteAgent`
- `DAGPlanExecutor`
- `CoordinatorAgent`

The v2 path should coexist without changing existing behavior.

## 4.2 Runtime overview

The intended flow is:

1. A v2 entrypoint chooses a primary agent, usually `build` or `plan`.
2. `SessionRuntime.run()` loads or creates the root session.
3. `AgentRunnerV2` runs a ReAct loop for the active session.
4. The model may:
   - call normal tools directly; or
   - call one or more `task` tools.
5. For each `task` call, `TaskToolV2` creates a child session and runs the target
   subagent in that child session.
6. Each child session finishes independently and returns a final summary string.
7. The parent session receives those summaries as tool results and continues its
   own reasoning.

## 4.3 Reuse vs. replacement

Phase 1 should reuse as much as possible:

- reuse the existing LLM backend layer;
- reuse the existing tool registry implementation shape;
- reuse current `TaskPolicy` ideas where practical;
- reuse the existing ReAct loop logic where practical.

But v2 should stop depending on the old orchestration split:

- no `v2` DAG executor;
- no `v2` multi-agent coordinator executor;
- no `v2` dedicated plan runtime.

## 5. Agent model

## 5.1 Built-in agents for phase 1

Phase 1 built-ins:

- `build`
  - `mode = primary`
  - full primary coding agent
- `plan`
  - `mode = primary`
  - read-only primary planning agent
- `explore`
  - `mode = subagent`
  - read-only child agent
- `general`
  - `mode = subagent`
  - general child coding agent

No aliases are preserved in v2. `reader / writer / verifier` do not exist in the
new registry.

## 5.2 Agent registry behavior

`AgentRegistryV2` should provide:

- lookup by name;
- filtering by `mode`;
- filtering hidden vs. visible agents;
- prompt/config retrieval;
- future support for config-backed custom agents.

The `task` tool description should be generated from the currently visible
subagents in the registry, so the model sees what child agents are available.

## 5.3 Suggested built-in permissions

Phase 1 default profiles:

- `build`
  - broad tool access within existing workspace boundaries
- `plan`
  - read-only tools
- `explore`
  - read-only tools
- `general`
  - read + write + command tools, but still capped by parent permissions

These profiles should be represented as declarative config, not hard-coded
branching in the runtime loop.

## 6. Session model

## 6.1 Session lifecycle

Each session, including child sessions, should move through explicit states:

- `queued`
- `running`
- `completed`
- `failed`
- `archived`

Phase 1 only needs create, update status, append messages, and append summary.

## 6.2 Session record

Minimum session fields:

- `id`
- `parent_id`
- `root_id`
- `agent_name`
- `mode`
- `title`
- `status`
- `repo_path`
- `created_at`
- `updated_at`
- `completed_at`
- `summary`
- `error`

Notes:

- `parent_id` is `NULL` for a root session.
- `root_id` points to the top-level ancestor for fast grouping.
- `summary` stores the final assistant conclusion used for parent result return.

## 6.3 Session messages

Messages should be stored separately from the session row.

Minimum message fields:

- `id`
- `session_id`
- `role`
- `content`
- `tool_call_id`
- `tool_name`
- `created_at`

Phase 1 only requires enough structure to reconstruct the session's own history.

## 6.4 Session metadata

Phase 1 should also persist lightweight metadata for debugging and future UI:

- `task_description`
- `subagent_type`
- `parent_tool_call_id`
- `run_kind` such as `root` or `task_child`

This can live in a JSON column or a separate metadata table. The exact storage
layout can be finalized at implementation time.

## 7. SQLite session store

## 7.1 Why SQLite now

SQLite is required in phase 1 because child sessions are first-class objects from
the start. An in-memory-only implementation would force a later rewrite of the
runtime's identity, lifecycle, and lookup semantics.

## 7.2 Minimum tables

Phase 1 should start with:

- `sessions`
- `session_messages`

Optional but recommended if cheap:

- `session_events`

`session_events` is not required for the first working chain, because existing
event logging can remain the primary observability path until v2 grows its own
event stream.

## 7.3 Store API

`SessionStore` should expose a small API:

- `create_session(...) -> SessionRecord`
- `get_session(session_id) -> SessionRecord | None`
- `list_child_sessions(parent_id) -> list[SessionRecord]`
- `append_message(...)`
- `update_status(...)`
- `set_summary(...)`
- `touch_session(session_id)`

Phase 1 does not need complex search or archival APIs.

## 8. Task tool v2

## 8.1 Tool contract

The v2 `task` tool accepts exactly these parameters:

- `description`
- `subagent_type`
- `prompt`

No additional fields are introduced in phase 1.

Suggested schema:

```json
{
  "type": "object",
  "properties": {
    "description": { "type": "string" },
    "subagent_type": { "type": "string" },
    "prompt": { "type": "string" }
  },
  "required": ["description", "subagent_type", "prompt"]
}
```

## 8.2 Execution behavior

For each tool call, `TaskToolV2` should:

1. validate `subagent_type` against the v2 registry;
2. derive the child agent config;
3. derive the child permission profile from parent + child type;
4. create a persisted child session row;
5. append the initial child user message using `prompt`;
6. run the child session through `SessionRuntime`;
7. capture the child final summary;
8. return a tool result containing the summary text and child session id.

Even if the parent prompt only uses the summary, the child session id should
still be available in the tool result payload for future inspection hooks.

## 8.3 Parallel semantics

If the model emits multiple `task` tool calls in one turn, the runtime should
treat them as parallel-capable by default.

Phase 1 guidance:

- do not add a separate `task_batch`;
- do not implement DAG ordering;
- do not implement worktree isolation;
- allow shared-workspace child execution;
- let the model decide whether sequencing is necessary.

## 9. Parent and child context rules

## 9.1 Parent context

The parent session behaves like a normal ReAct session. It keeps its own history,
system prompt, and tool results.

## 9.2 Child context

A child session starts clean. It does not inherit the full parent conversation
history. It receives:

- the child agent's own system prompt;
- the child session's own stored messages;
- one initial user message equal to `task.prompt`.

This clean-slate behavior is intentional. If the parent needs to pass context, it
must include that context explicitly in `task.prompt`.

## 9.3 Parent result ingestion

The parent should receive only the child's final summary as the tool result text.

Example result shape:

```json
{
  "output": "Completed the requested refactor and updated tests.",
  "session_id": "01H..."
}
```

The prompt-facing content should remain summary-first.

## 10. Permissions

## 10.1 Phase 1 model

Phase 1 permission flow is intentionally simple:

1. the active parent session has an effective permission profile;
2. the selected child agent has a default profile;
3. the child profile is computed as parent-constrained child defaults;
4. tools inside the child run under that narrowed profile.

This avoids introducing a second independent dispatch-permission framework in
phase 1.

## 10.2 Permission outcomes

The runtime should stay compatible with three permission outcomes:

- `allow`
- `ask`
- `deny`

Phase 1 only needs enough structure so that the child-session architecture does
not block later introduction of explicit approval prompts.

## 11. V2 entrypoint strategy

## 11.1 Isolation from old runtime

The existing runtime paths must remain untouched during phase 1.

V2 should be introduced through a separate path such as:

- a new CLI mode;
- a new chat mode;
- or a dedicated experimental entrypoint.

The key requirement is isolation:

- old mode behavior stays stable;
- v2 can evolve quickly without compatibility pressure.

## 11.2 Suggested entrypoint shape

Phase 1 can expose something like:

- `mode = v2-build`
- `mode = v2-plan`

Internally both would call the same `SessionRuntime.run()` entrypoint, differing
only by selected primary agent config.

## 12. Phase 1 implementation plan

## 12.1 Deliverable

A minimal working chain that proves the architecture:

1. create a root v2 session;
2. run a primary agent in that session;
3. let the model call `task`;
4. persist a child session in SQLite;
5. run `explore` or `general` in the child session;
6. persist child messages and summary;
7. return the summary to the parent session;
8. let the parent continue and finish.

## 12.2 Required modules

Expected new modules or equivalent:

- `agent/v2/session_runtime.py`
- `agent/v2/session_store.py`
- `agent/v2/agent_registry.py`
- `agent/v2/task_tool.py`
- `agent/v2/models.py`

Exact filenames can vary, but the separation of concerns should remain.

## 12.3 Recommended build order

1. define persisted session models and SQLite schema;
2. implement `SessionStore`;
3. implement built-in v2 agent registry;
4. implement minimal `SessionRuntime.run()`;
5. implement `task` tool using child session creation;
6. add a small isolated v2 entrypoint;
7. add tests for parent/child persistence and summary return.

## 13. Interfaces

## 13.1 SessionRuntime

Illustrative interface:

```python
class SessionRuntime:
    def __init__(self, store, backend, registry, tool_registry, config):
        ...

    def run(
        self,
        session_id: str,
        *,
        agent_name: str,
        messages: list[dict[str, str]] | None = None,
    ) -> RunResult:
        ...
```

Key expectations:

- if `messages` are provided, they are appended to the target session first;
- runtime loads the session's own history from storage;
- runtime executes exactly one agent loop for that session;
- runtime persists outputs back to the store.

## 13.2 SessionStore

Illustrative interface:

```python
class SessionStore:
    def create_session(
        self,
        *,
        agent_name: str,
        mode: str,
        repo_path: str,
        title: str,
        parent_id: str | None = None,
    ) -> SessionRecord:
        ...
```

## 13.3 TaskToolV2

Illustrative interface:

```python
class TaskToolV2(BaseTool):
    name = "task"

    def execute(self, params: dict[str, object]) -> ToolResult:
        ...
```

Expected behavior:

- no direct knowledge of old `CoordinatorAgent`;
- no `spawn_parallel` companion;
- no DAG planner coupling.

## 14. Sequence diagrams

## 14.1 Root session without child delegation

```text
User -> V2 Entrypoint -> SessionStore.create(root)
V2 Entrypoint -> SessionRuntime.run(root, agent=build)
SessionRuntime -> Model
Model -> regular tools
regular tools -> SessionRuntime
SessionRuntime -> SessionStore.set_summary(root)
SessionRuntime -> User
```

## 14.2 Root session with one child session

```text
User -> V2 Entrypoint -> root session
root session -> SessionRuntime.run(agent=build)
build -> task(description, subagent_type, prompt)
task -> SessionStore.create(child, parent=root)
task -> SessionRuntime.run(child, agent=subagent_type)
child -> normal tools
child -> SessionStore.set_summary(child)
task -> parent tool result(summary, session_id)
parent -> continue reasoning
parent -> finish
```

## 14.3 Multiple child sessions in one turn

```text
parent model response
  -> task(...)
  -> task(...)
  -> task(...)

runtime
  -> create child A/B/C
  -> run child A/B/C in parallel
  -> collect summaries
  -> append tool results to parent history
  -> continue parent loop
```

## 15. Testing strategy for phase 1

Minimum required tests:

- creating a root session persists a SQLite row;
- creating a child session sets `parent_id` and `root_id` correctly;
- child session stores its own messages separately from the parent;
- `task` tool rejects unknown `subagent_type`;
- `task` tool returns child summary text to the parent;
- multiple `task` calls in one step can execute without requiring a batch tool;
- `plan` primary agent is read-only in v2 registry;
- `explore` child agent is read-only in v2 registry.

Phase 1 does not require parity tests with legacy `dag` or `multi-agent`.

## 16. Migration after phase 1

If phase 1 succeeds, later stages can add:

- richer session event storage;
- parent/child navigation in the UI;
- configurable user-defined agents from JSON or Markdown;
- explicit approval prompts for `ask` permissions;
- session resume and continuation commands;
- eventual consolidation of old entrypoints into the unified runtime.

The important constraint is that phase 1 must already use the same core model:

- one runtime;
- persisted child sessions;
- native `task` delegation;
- agent behavior defined by registry config.

That foundation should not need to be redesigned later.
