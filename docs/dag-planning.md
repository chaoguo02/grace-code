# DAG Planning Notes

This document captures the current DAG Plan-and-Execute model and the guardrails around it.

## Planner Responsibilities

`DAGPlanner` turns a user goal into a structured JSON DAG plan. It:

- runs in a read-only planning phase;
- asks the model for JSON only;
- accepts both `plan` and `tasks` arrays;
- accepts both `depends_on` and `dependencies`;
- normalizes model-generated IDs to `task_1`, `task_2`, ...;
- validates the DAG and fills reverse `dependents` links.

## SubTask Model

Each DAG node is a `SubTask` with:

- `id`: normalized ID used by execution;
- `original_id`: raw ID from the model, kept for traceability;
- `type`: semantic task type;
- `status`: lifecycle state;
- `depends_on` / `dependents`: dependency graph;
- `start_time_ms`, `end_time_ms`, `duration_ms`: timing data;
- `result_summary`, `error`, `skip_reason`: execution outcome.

## SubTask Types

Allowed types:

- `planning`: planning or decision nodes;
- `file_read`: information gathering;
- `file_write`: file edits;
- `command`: shell/test commands;
- `analysis`: intermediate reasoning;
- `verification`: final checks.

## Tool Permissions

DAG execution enforces a hard tool allowlist per `SubTaskType`.

- read/analysis/planning nodes get read-only tools;
- file_write nodes get read-only plus write tools;
- command and verification nodes get read-only plus shell/test tools.

This is enforced by filtered `ToolRegistry` instances, not only by prompt instructions.

## Parallel Execution

Same-layer parallelism is intentionally conservative.

- Enabled by default only when the whole layer is `planning`, `file_read`, or `analysis`.
- `verification` and `command` parallelism have config switches but default to off.
- file-write parallelism remains blocked unless future worktree/path conflict handling is added.

## Replan

Replan support exists but is disabled by default.

```yaml
plan:
  enable_replan: false
  max_replans: 1
```

When enabled, a failed DAG can generate a new plan for remaining work only. Replan context includes completed, failed, and skipped subtasks.

## Observability

DAG execution records:

- subtask start / complete / failed / skipped events;
- replan-generated events;
- DAG graph events with Mermaid text;
- critical path summary;
- per-type task count, duration, and failure count.

Mermaid graphs are written to EventLog instead of the final summary to keep user-facing output concise.

## Future Work

- robust file conflict detection for write-node parallelism;
- worktree isolation for parallel file edits;
- config-driven tool permission matrix;
- persisted graph artifacts such as `logs/<task_id>_dag.mmd`;
- formal pytest coverage if the test suite is restored.
