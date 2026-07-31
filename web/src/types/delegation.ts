import type { WsDelegationEvent, WsMessage } from "./events";

export type DelegationTaskStatus =
  | "queued"
  | "running"
  | "completed"
  | "partial"
  | "failed"
  | "cancelled"
  | "blocked"
  | "interrupted"
  | "no_findings"
  | "budget_exhausted"
  | "superseded";

export type DelegationRunPhase =
  | "planned"
  | "executing"
  | "synthesizing"
  | "awaiting_integration"
  | "integrating"
  | "integration_failed"
  | "awaiting_verification"
  | "verifying"
  | "verification_failed"
  | "recovery_required"
  | "partial"
  | "failed"
  | "cancelled"
  | "completed";
export type DelegationRunStatus =
  | "planned"
  | "running"
  | "completed"
  | "partial"
  | "failed"
  | "cancelled"
  | "budget_exhausted";

export interface TaskState {
  taskId: string;
  agentType: string;
  childSessionId?: string;
  generation?: number;
  dependencies: string[];
  status: DelegationTaskStatus;
  error?: string;
  integrationStatus?: string;
  tokensUsed?: number;
  durationMs?: number;
  updatedAt?: string;
  schemaVersion?: number;
  acceptance?: string[];
  // Phase 4: resource governance fields
  resourceRequested?: Record<string, number>;
  resourceGranted?: Record<string, number>;
  resourceConsumed?: Record<string, number>;
  resourceRefunded?: Record<string, number>;
  resourceQueuePosition?: number;
  resourceWaitTimeS?: number;
  resourceOutcome?: string;
}

export interface ResourceWorkerState {
  limit: number;
  reserved: number;
  consumed: number;
  available: number;
  queued: number;
  pressure: string;
}

export interface ResourceState {
  mode: string;
  worker: ResourceWorkerState;
  active_leases: number;
  queue_depth: number;
}

export interface DelegationRunState {
  runId: string;
  topology: string;
  phase: DelegationRunPhase;
  status: DelegationRunStatus;
  taskCount: number;
  budget?: Record<string, unknown>;
  reportCount: number;
  integrationStatus?: string;
  verification?: Record<string, unknown>;
  tasks: Record<string, TaskState>;
  createdAt?: string;
  updatedAt?: string;
  updatedSequence?: number;
}

export type DelegationRuns = Record<string, DelegationRunState>;

const EVENT_TYPES = new Set<WsDelegationEvent["type"]>([
  "delegation_planned",
  "delegation_task_queued",
  "delegation_task_started",
  "delegation_task_reported",
  "delegation_task_failed",
  "delegation_task_blocked",
  "delegation_task_retrying",
  "delegation_phase_changed",
  "delegation_integration_started",
  "delegation_integration_completed",
  "delegation_verification_started",
  "delegation_verification_completed",
  "delegation_synthesis_started",
  "delegation_completed",
  "delegation_budget_exhausted",
  "delegation_resource_queued",
  "delegation_resource_granted",
  "delegation_resource_reconciled",
  "delegation_resource_released",
  "delegation_resource_cancelled",
  "delegation_resource_capacity_timeout",
  "delegation_resource_shutdown",
  "delegation_resource_rejected",
]);

const TASK_STATUS_RANK: Record<DelegationTaskStatus, number> = {
  queued: 0,
  running: 1,
  completed: 2,
  partial: 2,
  failed: 2,
  cancelled: 2,
  blocked: 2,
  interrupted: 2,
  no_findings: 2,
  budget_exhausted: 2,
  superseded: 2,
};

const PHASE_RANK: Record<DelegationRunPhase, number> = {
  planned: 0,
  executing: 1,
  synthesizing: 2,
  awaiting_integration: 3,
  integrating: 4,
  integration_failed: 7,
  awaiting_verification: 5,
  verifying: 6,
  verification_failed: 7,
  recovery_required: 7,
  partial: 7,
  failed: 7,
  cancelled: 7,
  completed: 7,
};

export function isDelegationEvent(event: WsMessage): event is WsDelegationEvent {
  return EVENT_TYPES.has(event.type as WsDelegationEvent["type"]);
}

function fields(event: WsDelegationEvent) {
  return { ...(event.payload || {}), ...event };
}

function laterTimestamp(current?: string, incoming?: string): string | undefined {
  if (!current) return incoming;
  if (!incoming) return current;
  return incoming > current ? incoming : current;
}

function advancePhase(current: DelegationRunPhase, incoming: DelegationRunPhase): DelegationRunPhase {
  return PHASE_RANK[incoming] > PHASE_RANK[current] ? incoming : current;
}

function normalizePhase(value: string | undefined, fallback: DelegationRunPhase): DelegationRunPhase {
  return value && value in PHASE_RANK ? value as DelegationRunPhase : fallback;
}

function normalizeTaskStatus(status: string | undefined, fallback: DelegationTaskStatus): DelegationTaskStatus {
  if (
    status === "completed" || status === "partial" || status === "failed" ||
    status === "cancelled" || status === "blocked" || status === "interrupted" ||
    status === "no_findings" || status === "budget_exhausted" || status === "superseded"
  ) return status;
  if (status === "running" || status === "queued") return status;
  return fallback;
}

function advanceTaskStatus(current: DelegationTaskStatus, incoming: DelegationTaskStatus): DelegationTaskStatus {
  if (TASK_STATUS_RANK[incoming] < TASK_STATUS_RANK[current]) return current;
  if (TASK_STATUS_RANK[incoming] === TASK_STATUS_RANK[current] && current !== incoming) return current;
  return incoming;
}

/** Apply one event without mutating input. Safe for duplicates and out-of-order replay. */
export function reduceDelegationEvent(runs: DelegationRuns, event: WsMessage): DelegationRuns {
  if (!isDelegationEvent(event)) return runs;
  const data = fields(event);
  const runId = data.delegation_run_id || data.run_id;
  if (!runId) return runs;

  const previous = runs[runId] || {
    runId,
    topology: "unknown",
    phase: "planned" as const,
    status: "planned" as const,
    taskCount: 0,
    reportCount: 0,
    tasks: {},
  };
  let run: DelegationRunState = {
    ...previous,
    tasks: previous.tasks,
    createdAt: previous.createdAt || event.timestamp,
    updatedAt: laterTimestamp(previous.updatedAt, event.timestamp),
    updatedSequence: Math.max(
      previous.updatedSequence ?? -1,
      (event as WsMessage & { seq?: number }).seq ?? event.sequence ?? -1,
    ),
  };

  if (event.type === "delegation_planned") {
    run = {
      ...run,
      topology: data.topology || run.topology,
      taskCount: data.task_count ?? run.taskCount,
      budget: data.budget || run.budget,
    };
  } else if (event.type === "delegation_synthesis_started") {
    run = {
      ...run,
      phase: advancePhase(run.phase, "synthesizing"),
      status: run.phase === "completed" ? run.status : "running",
      reportCount: Math.max(run.reportCount, data.report_count ?? 0),
    };
  } else if (event.type === "delegation_phase_changed") {
    const phase = normalizePhase(data.phase, run.phase);
    const restarting = data.reason === "retry" || data.reason === "resume" || data.reason === "snapshot_reconciliation";
    run = {
      ...run,
      phase: restarting ? phase : advancePhase(run.phase, phase),
      status: restarting ? "running" : run.status,
    };
  } else if (event.type === "delegation_integration_started") {
    run = { ...run, phase: "integrating", status: "running" };
  } else if (event.type === "delegation_integration_completed") {
    const phase = normalizePhase(data.phase, "awaiting_verification");
    run = {
      ...run,
      phase: advancePhase(run.phase, phase),
      integrationStatus: data.integration_status || data.status || run.integrationStatus,
    };
  } else if (event.type === "delegation_verification_started") {
    run = { ...run, phase: "verifying", status: "running" };
  } else if (event.type === "delegation_verification_completed") {
    const phase = normalizePhase(data.phase, run.phase);
    run = {
      ...run,
      phase: advancePhase(run.phase, phase),
      verification: data.verification || run.verification,
    };
  } else if (event.type === "delegation_completed") {
    const status =
      data.status === "partial" || data.status === "failed" || data.status === "cancelled"
        ? data.status
        : "completed";
    run = {
      ...run,
      phase: normalizePhase(data.phase, "completed"),
      status,
      reportCount: Math.max(run.reportCount, data.report_count ?? 0),
    };
  } else if (event.type === "delegation_budget_exhausted") {
    run = { ...run, phase: "completed", status: "budget_exhausted" };
  } else {
    const taskId = data.task_id;
    if (!taskId) return runs;
    const previousTask = run.tasks[taskId] || {
      taskId,
      agentType: data.agent_type || "agent",
      dependencies: data.dependencies || [],
      status: "queued" as const,
    };
    let incomingStatus: DelegationTaskStatus = "queued";
    if (event.type === "delegation_task_started") incomingStatus = "running";
    if (event.type === "delegation_task_reported") incomingStatus = normalizeTaskStatus(data.status, "completed");
    if (event.type === "delegation_task_failed") incomingStatus = normalizeTaskStatus(data.status, "failed");
    if (event.type === "delegation_task_blocked") incomingStatus = "blocked";
    const task: TaskState = {
      ...previousTask,
      agentType: data.agent_type || previousTask.agentType,
      childSessionId: data.child_session_id || previousTask.childSessionId,
      generation: data.generation ?? previousTask.generation,
      dependencies: data.dependencies || previousTask.dependencies,
      status: data.reason === "snapshot_reconciliation"
        ? incomingStatus
        : advanceTaskStatus(previousTask.status, incomingStatus),
      error: data.error || data.reason || previousTask.error,
      integrationStatus: data.integration_status || previousTask.integrationStatus,
      tokensUsed: data.tokens_used ?? previousTask.tokensUsed,
      durationMs: data.duration_ms ?? previousTask.durationMs,
      resourceRequested: event.type === "delegation_resource_queued"
        ? data.resources
        : previousTask.resourceRequested,
      resourceGranted: event.type === "delegation_resource_granted"
        ? data.resources
        : previousTask.resourceGranted,
      resourceConsumed: (
        event.type === "delegation_resource_reconciled"
        || event.type === "delegation_resource_released"
      )
        ? data.actual
        : previousTask.resourceConsumed,
      resourceRefunded: (
        event.type === "delegation_resource_reconciled"
        || event.type === "delegation_resource_released"
      )
        ? Object.fromEntries(Object.entries(data.resources || {}).map(
          ([kind, reserved]) => [
            kind,
            Math.max(0, reserved - (data.actual?.[kind] || 0)),
          ],
        ))
        : previousTask.resourceRefunded,
      resourceQueuePosition: data.queue_position ?? previousTask.resourceQueuePosition,
      resourceWaitTimeS: data.wait_time_s ?? previousTask.resourceWaitTimeS,
      resourceOutcome: data.outcome ?? previousTask.resourceOutcome,
      updatedAt: laterTimestamp(previousTask.updatedAt, event.timestamp),
    };
    const isTerminal = TASK_STATUS_RANK[task.status] === 2;
    run = {
      ...run,
      phase: event.type === "delegation_task_retrying"
        ? "executing"
        : advancePhase(run.phase, "executing"),
      status: event.type === "delegation_task_retrying"
        ? "running"
        : run.phase === "completed" ? run.status : "running",
      taskCount: Math.max(run.taskCount, Object.keys(run.tasks).length + (run.tasks[taskId] ? 0 : 1)),
      reportCount: Math.max(run.reportCount, Object.values(run.tasks).filter((item) => TASK_STATUS_RANK[item.status] === 2).length + (isTerminal && TASK_STATUS_RANK[previousTask.status] < 2 ? 1 : 0)),
      tasks: { ...run.tasks, [taskId]: task },
    };
  }

  return { ...runs, [runId]: run };
}

/** Rebuild delegation state from durable events, optionally merging into live state. */
export function rebuildDelegationRuns(events: WsMessage[], initial: DelegationRuns = {}): DelegationRuns {
  return events.reduce(reduceDelegationEvent, initial);
}
