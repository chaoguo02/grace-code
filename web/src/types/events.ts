/**
 * Typed WebSocket event discriminated union.
 *
 * Mirrors server/events.py dataclass shapes.  The 'type' field is the
 * discriminator — handler maps use Extract<> for type narrowing.
 *
 * Pattern: CorvidAgent shared type layer + react-socket typed events.
 * Source: https://github.com/CorvidLabs/corvid-agent/issues/957
 */

// ── EventEnvelope base ────────────────────────────────────────────────────

/** Fields injected by the backend on every WS event. All optional for backward compat. */
export interface EventEnvelope {
  session_id?: string;
  run_id?: string;
  turn_id?: string;
  event_id?: string;
  sequence?: number;
  block_id?: string;
  tool_call_id?: string;
  turn_index?: number;
}

// ── Status ──────────────────────────────────────────────────────────────

export interface WsStatusEvent extends EventEnvelope {
  type: "status";
  status: "running" | "completed" | "failed" | "finish" | "gave_up" | "cancelled" | "compacted";
  message?: string;
  error?: string;
  result?: { summary?: string; steps_taken?: number; total_tokens?: number };
  content?: string;
  timestamp?: string;
  step?: number;
  duration_ms?: number;
  token_estimate?: number;
  child_session_id?: string;
}

// ── Thought / Reflection ────────────────────────────────────────────────

export interface WsThoughtEvent extends EventEnvelope {
  type: "thought";
  content: string;
  timestamp?: string;
  step?: number;
  duration_ms?: number;
  token_estimate?: number;
  child_session_id?: string;
}

export interface WsThoughtDeltaEvent extends EventEnvelope {
  type: "thought_delta";
  text: string;
  timestamp?: string;
  step?: number;
  child_session_id?: string;
}

export interface WsReflectionEvent extends EventEnvelope {
  type: "reflection";
  content: string;
  timestamp?: string;
  step?: number;
  duration_ms?: number;
  token_estimate?: number;
}

// ── Tool call / Observation ─────────────────────────────────────────────

export interface WsToolCallEvent extends EventEnvelope {
  type: "tool_call";
  name: string;
  params?: Record<string, unknown>;
  id?: string;
  timestamp?: string;
  step?: number;
  duration_ms?: number;
  token_estimate?: number;
  child_session_id?: string;
}

export interface WsObservationEvent extends EventEnvelope {
  type: "observation";
  tool_name?: string;
  output?: string;
  error?: string;
  status?: string;
  id?: string;
  paired?: boolean;
  diff?: string;
  timestamp?: string;
  step?: number;
  duration_ms?: number;
  token_estimate?: number;
  child_session_id?: string;
}

// ── Subagent ────────────────────────────────────────────────────────────

export interface WsSubagentStartEvent extends EventEnvelope {
  type: "subagent_start";
  child_session_id: string;
  agent_name?: string;
  timestamp?: string;
  step?: number;
}

export interface WsSubagentStopEvent extends EventEnvelope {
  type: "subagent_stop";
  child_session_id: string;
  status?: string;
  timestamp?: string;
  step?: number;
}

// ── Multi-agent delegation ─────────────────────────────────────────────

export interface DelegationBudgetPayload {
  available_tokens?: number;
  parent_reserve_tokens?: number;
  recovery_reserve_tokens?: number;
  worker_pool_tokens?: number;
  max_concurrent?: number;
  [key: string]: unknown;
}

export interface DelegationEventPayload {
  delegation_run_id?: string;
  topology?: string;
  task_count?: number;
  budget?: DelegationBudgetPayload;
  task_id?: string;
  generation?: number;
  agent_type?: string;
  child_session_id?: string;
  phase?: string;
  previous_phase?: string;
  status?: string;
  integration_status?: string;
  verification?: Record<string, unknown>;
  action?: string;
  dependencies?: string[];
  changed_files?: string[];
  report_count?: number;
  tokens_used?: number;
  duration_ms?: number;
  reason?: string;
  error?: string;
}

/**
 * Delegation events may arrive flattened over WebSocket or in the durable
 * fallback shape `{ type, payload, timestamp }`. Keep both forms optional so
 * older servers and replayed traces remain readable.
 */
interface WsDelegationEventBase extends EventEnvelope, DelegationEventPayload {
  payload?: DelegationEventPayload;
  timestamp?: string;
}

export interface WsDelegationPlannedEvent extends WsDelegationEventBase {
  type: "delegation_planned";
}

export interface WsDelegationTaskQueuedEvent extends WsDelegationEventBase {
  type: "delegation_task_queued";
}

export interface WsDelegationTaskStartedEvent extends WsDelegationEventBase {
  type: "delegation_task_started";
}

export interface WsDelegationTaskReportedEvent extends WsDelegationEventBase {
  type: "delegation_task_reported";
}

export interface WsDelegationTaskFailedEvent extends WsDelegationEventBase {
  type: "delegation_task_failed";
}

export interface WsDelegationTaskBlockedEvent extends WsDelegationEventBase {
  type: "delegation_task_blocked";
}

export interface WsDelegationTaskRetryingEvent extends WsDelegationEventBase {
  type: "delegation_task_retrying";
}

export interface WsDelegationPhaseChangedEvent extends WsDelegationEventBase {
  type: "delegation_phase_changed";
}

export interface WsDelegationIntegrationStartedEvent extends WsDelegationEventBase {
  type: "delegation_integration_started";
}

export interface WsDelegationIntegrationCompletedEvent extends WsDelegationEventBase {
  type: "delegation_integration_completed";
}

export interface WsDelegationVerificationStartedEvent extends WsDelegationEventBase {
  type: "delegation_verification_started";
}

export interface WsDelegationVerificationCompletedEvent extends WsDelegationEventBase {
  type: "delegation_verification_completed";
}

export interface WsDelegationSynthesisStartedEvent extends WsDelegationEventBase {
  type: "delegation_synthesis_started";
}

export interface WsDelegationCompletedEvent extends WsDelegationEventBase {
  type: "delegation_completed";
}

export interface WsDelegationBudgetExhaustedEvent extends WsDelegationEventBase {
  type: "delegation_budget_exhausted";
}

export type WsDelegationEvent =
  | WsDelegationPlannedEvent
  | WsDelegationTaskQueuedEvent
  | WsDelegationTaskStartedEvent
  | WsDelegationTaskReportedEvent
  | WsDelegationTaskFailedEvent
  | WsDelegationTaskBlockedEvent
  | WsDelegationTaskRetryingEvent
  | WsDelegationPhaseChangedEvent
  | WsDelegationIntegrationStartedEvent
  | WsDelegationIntegrationCompletedEvent
  | WsDelegationVerificationStartedEvent
  | WsDelegationVerificationCompletedEvent
  | WsDelegationSynthesisStartedEvent
  | WsDelegationCompletedEvent
  | WsDelegationBudgetExhaustedEvent;

// ── Approval ────────────────────────────────────────────────────────────

export interface WsApprovalRequiredEvent extends EventEnvelope {
  type: "approval_required";
  request_id: string;
  tool_name: string;
  params?: Record<string, unknown>;
  thought?: string;
  decision_reason?: string;
  tool_use_id?: string;
  permission_mode?: string;
  risk_level?: string;
  timestamp?: string;
  step?: number;
}

export interface WsApprovalTimeoutEvent extends EventEnvelope {
  type: "approval_timeout";
  request_id: string;
  timestamp?: string;
}

export interface WsApprovalResolvedEvent extends EventEnvelope {
  type: "approval_resolved";
  request_id: string;
  tool_name: string;
  decision: "allow_once" | "always_allow" | "deny" | string;
  note?: string;
  updated_input?: boolean;
  wait_ms?: number;
  timestamp?: string;
}

// ── Plan ────────────────────────────────────────────────────────────────

export interface WsPlanReadyEvent extends EventEnvelope {
  type: "plan_ready";
  plan_text?: string;
  contract?: Record<string, unknown> | null;
  revision?: number;
  max_revisions?: number;
  result?: { summary?: string; steps_taken?: number; total_tokens?: number };
  timestamp?: string;
  step?: number;
}

// ── Worktree ────────────────────────────────────────────────────────────

export interface WsWorktreeResolvedEvent extends EventEnvelope {
  type: "worktree_resolved";
  child_session_id: string;
  action: string;
  status: string;
  message?: string;
  timestamp?: string;
  step?: number;
}

export interface WsReviewUpdatedEvent extends EventEnvelope {
  type: "review_updated";
  job_id: string;
  status: string;
  task_states: Record<string, string>;
  finding_count: number;
  workspace_revision: string;
  timestamp?: string;
}

// ── Memory activity ─────────────────────────────────────────────────────

export interface WsMemoryRecallEvent extends EventEnvelope {
  type: "memory_recall";
  injected_count: number;
  candidate_count: number;
  omitted_count: number;
  top_names: string[];
  timestamp?: string;
}

export interface WsMemoryWrittenEvent extends EventEnvelope {
  type: "memory_written";
  name: string;
  description: string;
  source: string;
  confidence: number;
  timestamp?: string;
}

// ── Assistant text streaming ─────────────────────────────────────────────

export interface WsAssistantTextStartEvent extends EventEnvelope {
  type: "assistant_text_start";
  block_id: string;
  timestamp?: string;
}

export interface WsAssistantTextDeltaEvent extends EventEnvelope {
  type: "assistant_text_delta";
  text: string;
  block_id: string;
  timestamp?: string;
}

export interface WsAssistantTextEndEvent extends EventEnvelope {
  type: "assistant_text_end";
  block_id: string;
  timestamp?: string;
}

export interface WsAssistantTextAbortedEvent extends EventEnvelope {
  type: "assistant_text_aborted";
  block_id: string;
  reason: string;
  timestamp?: string;
}

// ── Run lifecycle ────────────────────────────────────────────────────────

export interface WsRunStartedEvent extends EventEnvelope {
  type: "run_started";
  run_id: string;
  turn_id: string;
  turn_index: number;
  timestamp?: string;
}

export interface RunVerificationCheck {
  name: string;
  status: "passed" | "failed" | "skipped" | "unavailable" | string;
  command?: string;
  detail?: string;
  duration_ms?: number;
}

export interface RunVerification {
  status: "not_applicable" | "verified" | "unverified" | "unavailable" | "failed" | string;
  reason: string;
  checks?: RunVerificationCheck[];
}

export interface RunWorkspaceDelta {
  has_changes?: boolean;
  changed_files?: string[];
  patch_available?: boolean;
  source?: "git" | "tool_journal" | string;
  is_run_scoped?: boolean;
}

export interface WsRunTerminalEvent extends EventEnvelope {
  type: "run_terminal";
  run_id: string;
  turn_id: string;
  turn_index: number;
  status: "completed" | "failed" | "cancelled";
  summary: string;
  steps_taken: number;
  total_tokens: number;
  error?: string;
  termination_reason?: string;
  verification_status?: string;
  verification_reason?: string;
  verification?: RunVerification;
  workspace_delta?: RunWorkspaceDelta;
  timestamp?: string;
}

// ── Discriminated union ─────────────────────────────────────────────────

export type WsMessage =
  | WsStatusEvent
  | WsThoughtEvent
  | WsThoughtDeltaEvent
  | WsReflectionEvent
  | WsToolCallEvent
  | WsObservationEvent
  | WsSubagentStartEvent
  | WsSubagentStopEvent
  | WsDelegationEvent
  | WsApprovalRequiredEvent
  | WsApprovalTimeoutEvent
  | WsApprovalResolvedEvent
  | WsPlanReadyEvent
  | WsWorktreeResolvedEvent
  | WsReviewUpdatedEvent
  | WsMemoryRecallEvent
  | WsMemoryWrittenEvent
  | WsAssistantTextStartEvent
  | WsAssistantTextDeltaEvent
  | WsAssistantTextEndEvent
  | WsAssistantTextAbortedEvent
  | WsRunStartedEvent
  | WsRunTerminalEvent;

// ── Typed handler utility ───────────────────────────────────────────────

/** Narrow a WsMessage to a specific subtype. */
export type WsMessageOfType<T extends WsMessage["type"]> = Extract<WsMessage, { type: T }>;

// ── Transport-level envelope ────────────────────────────────────────────

/**
 * WsMessage with optional trace sequence number injected by the backend
 * transport layer (EventBus WebSocket broadcast + /timeline REST).
 *
 * ``seq`` is NOT a domain property of individual event types — it is
 * a per-session monotonic counter that the backend stamps on every
 * event at persist/broadcast time.  Use this type in store handlers
 * and transport adapters; render components should use ``WsMessage``.
 */
export type WsEnvelope = WsMessage & { seq?: number };
