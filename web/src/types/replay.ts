export interface ReplayValidationIssue {
  severity: "error" | "warning" | string;
  field: string;
  message: string;
}

export interface ReplayToolVisibility {
  name: string;
  visible: boolean;
  source?: string;
  reason?: string;
  schema?: Record<string, unknown>;
}

export interface ReplayToolExecution {
  tool_name: string;
  tool_call_id?: string;
  params?: Record<string, unknown>;
  success: boolean;
  output_summary?: string;
  error?: string;
  duration_ms?: number;
  outcome?: string;
}

export interface ReplayStep {
  step: number;
  runtime_decision: {
    action?: string;
    reason?: string;
    strip_tools?: boolean;
    inject_message?: string;
    terminate_reason?: string;
    terminate_status?: string;
    terminate_detail?: string;
  };
  visible_tools: ReplayToolVisibility[];
  model_action: {
    action_type?: string;
    thought?: string;
    message?: string;
    tool_calls?: Array<{
      id?: string;
      name?: string;
      params?: Record<string, unknown>;
    }>;
  };
  tool_executions: ReplayToolExecution[];
  outcome?: string;
  termination_reason?: string;
  termination_status?: string;
  validation: {
    valid: boolean;
    expected_position: number;
    issues: ReplayValidationIssue[];
  };
}

export interface ReplayRun {
  run_id: string;
  turn_id: string;
  turn_index: number;
  status: string;
  started_at: string;
  completed_at: string;
  contract_source:
    | "persisted_replay_run"
    | "reconstructed_from_steps"
    | "legacy_terminal_only"
    | string;
  evidence_complete: boolean;
  last_sequence: number;
  record: {
    version: number;
    run_id: string;
    task_id: string;
    session_id?: string;
    generation?: number;
    task?: Record<string, unknown>;
    provenance?: Record<string, unknown>;
    permission_snapshot?: Record<string, unknown>;
    runtime_snapshot?: Record<string, unknown>;
    visible_tools?: ReplayToolVisibility[];
    steps: ReplayStep[];
    termination_reason: string;
    termination_status: string;
    summary: string;
  };
  validation: {
    valid: boolean;
    schema_valid: boolean;
    complete: boolean;
    completeness_message: string;
    boundary_preserved: boolean;
    steps_validated: number;
    issues: ReplayValidationIssue[];
    event_step_count: number;
    record_step_count: number;
  };
  metrics: {
    tool_executions: number;
    failed_tools: number;
    visible_tool_peak: number;
    visibility_changes: number;
    strip_tools_decisions: number;
  };
}

export interface FailureTaxonomyEntry {
  reason: string;
  category: string;
  behavior: string;
  max_recovery_attempts: number;
  preserves_history: boolean;
  expected_status: string;
}

export interface SessionReplay {
  session_id: string;
  agent_name: string;
  runs: ReplayRun[];
  summary: {
    run_count: number;
    contract_count: number;
    valid_count: number;
    boundary_preserved_count: number;
    step_count: number;
    failed_tool_count: number;
  };
  failure_taxonomy: FailureTaxonomyEntry[];
  contract_version: number;
  disclosure: {
    source: string;
    historical_runs_may_be_reconstructed: boolean;
    reconstructed_provenance_is_not_inferred: boolean;
    tool_outputs_are_runtime_truncated: boolean;
  };
}

export interface ReplayExecutionAttempt {
  step: number;
  tool_call_id: string;
  tool_name: string;
  success: boolean;
  classification: string;
  attempt_count?: number;
  eventual_success?: boolean;
  error?: string;
  output_fingerprint?: string;
}

export interface ReplayExecution {
  id: string;
  session_id: string;
  run_id: string;
  status: "queued" | "running" | "completed" | "failed" | string;
  classification: "matched" | "expected_divergence" | "unexpected_divergence" | "blocked" | string;
  pinned: boolean;
  workspace_path?: string;
  diff?: string;
  error?: string;
  attempts?: ReplayExecutionAttempt[];
}
