/** Aggregate stats for a completed session */
export interface SessionStats {
  session_id: string;
  agent_name: string;
  total_steps: number;
  total_tokens: number;
  total_duration_ms: number;
  status: string;
  tool_summary: Record<string, number>;
  created_at: string;
}

/** One step in a session's execution log */
export interface StepLog {
  id: number;
  session_id: string;
  step_number: number;
  tool_name: string;
  tool_params: string;
  status: string;
  duration_ms: number;
  tokens: number;
  timestamp: string;
}

export interface ContextSnapshotStats {
  request_budget_tokens: number;
  estimated_total_tokens: number;
  system_tokens: number;
  project_tokens: number;
  memory_tokens: number;
  session_tokens: number;
  task_tokens: number;
  repo_map_tokens: number;
  artifact_summary_tokens: number;
  omitted_tokens: number;
  compact_triggered: boolean;
  compact_reason: string;
  compact_method: string;
  compact_truncated: boolean;
  compact_source_range?: [number, number] | null;
}

export interface ContextCapabilities {
  tool_names: string[];
  tool_count: number;
  mcp_tools: string[];
  mcp_servers: string[];
  active_skills: string[];
}

export interface ContextSnapshot {
  id: number;
  session_id: string;
  run_id: string;
  turn_id: string;
  step_number: number;
  request_kind: "primary" | "subagent" | "inherited" | string;
  stats: ContextSnapshotStats;
  capabilities: ContextCapabilities;
  created_at: string;
}

export interface ContextMemoryRecall {
  session_id: string;
  memory_name: string;
  source: string;
  score: number;
  reason: string;
  confidence: number;
  scope: string;
  injected: boolean;
  omitted_reason: string;
  turn_id: string;
  created_at: string;
  description?: string;
  type?: string;
  override?: string;
}

export interface SessionContextInspection {
  session_id: string;
  snapshots: ContextSnapshot[];
  memory_recalls: ContextMemoryRecall[];
  actual_usage: {
    tool_names: string[];
    mcp_tools: string[];
    skill_tool_used: boolean;
  };
  disclosure: {
    prompt_content_included: boolean;
    token_counts_are_estimates: boolean;
    snapshot_source: string;
  };
}

/** Daily aggregate stats */
export interface DailyRollup {
  date: string;
  session_count: number;
  total_tokens: number;
  total_duration_ms: number;
  tool_summary: Record<string, number>;
  status_summary: Record<string, number>;
}

/** One file diff from a session */
export interface SessionDiff {
  id: number;
  session_id: string;
  step_number: number;
  file_path: string;
  diff_content: string;
  status: "pending" | "approved" | "rejected";
  review_comment: string;
  created_at: string;
  /** Enriched fields from /api/diffs/pending */
  session_title?: string;
  session_agent?: string;
}
