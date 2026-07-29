import type { WsMessage } from "./events";
import { rebuildDelegationRuns, reduceDelegationEvent } from "./delegation";

function event(type: string, sequence: number, payload: Record<string, unknown>): WsMessage {
  return {
    type,
    sequence,
    timestamp: `2026-07-29T00:00:0${sequence}Z`,
    payload: { delegation_run_id: "run-1", ...payload },
  } as WsMessage;
}

const completed = event("delegation_completed", 4, {
  status: "completed",
  report_count: 1,
});
const started = event("delegation_task_started", 2, {
  task_id: "task-1",
  agent_type: "explore",
  child_session_id: "child-1",
});
const reported = event("delegation_task_reported", 3, {
  task_id: "task-1",
  agent_type: "explore",
  child_session_id: "child-1",
  status: "completed",
});

const replayed = rebuildDelegationRuns([completed, reported, started, reported]);
if (replayed["run-1"].phase !== "completed") {
  throw new Error("Out-of-order task events must not regress a completed run");
}
if (replayed["run-1"].tasks["task-1"].status !== "completed") {
  throw new Error("A late started event must not regress a terminal task");
}
if (replayed["run-1"].reportCount !== 1) {
  throw new Error("Duplicate reports must remain idempotent");
}

const cancelled = reduceDelegationEvent(
  {},
  event("delegation_completed", 1, { status: "cancelled" }),
);
if (cancelled["run-1"].status !== "cancelled") {
  throw new Error("Cancelled delegation runs must retain their terminal status");
}


const reopened = rebuildDelegationRuns([
  completed,
  event("delegation_task_retrying", 5, {
    task_id: "task-1:generation-1",
    agent_type: "explore",
    generation: 1,
    dependencies: [],
    status: "queued",
  }),
  event("delegation_phase_changed", 6, {
    phase: "executing",
    status: "running",
    reason: "retry",
  }),
]);
if (reopened["run-1"].phase !== "executing" || reopened["run-1"].status !== "running") {
  throw new Error("Retry must reopen a previously terminal delegation run");
}

const gated = rebuildDelegationRuns([
  event("delegation_integration_started", 1, { phase: "integrating", status: "running" }),
  event("delegation_integration_completed", 2, {
    phase: "awaiting_verification",
    integration_status: "applied",
  }),
  event("delegation_verification_started", 3, { phase: "verifying", status: "running" }),
  event("delegation_verification_completed", 4, {
    phase: "completed",
    status: "passed",
    verification: { status: "passed", checks: [] },
  }),
]);
if (gated["run-1"].phase !== "completed") {
  throw new Error("Integration and verification events must advance the gate phase");
}
if (gated["run-1"].verification?.status !== "passed") {
  throw new Error("Verification payload must survive replay");
}

const snapshotCorrected = rebuildDelegationRuns([
  reported,
  event("delegation_task_failed", 0, {
    task_id: "task-1",
    agent_type: "explore",
    status: "interrupted",
    reason: "snapshot_reconciliation",
  }),
]);
if (snapshotCorrected["run-1"].tasks["task-1"].status !== "interrupted") {
  throw new Error("Durable snapshot reconciliation must override stale live task state");
}
