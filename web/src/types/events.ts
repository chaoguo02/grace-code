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
  | WsApprovalRequiredEvent
  | WsApprovalTimeoutEvent
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
