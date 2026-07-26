export interface ReliabilityObjective {
  id: string;
  label: string;
  observed: number | null;
  target: number;
  comparator: "gte" | "lte" | string;
  met: boolean | null;
  detail: string;
}

export interface ReliabilityTool {
  name: string;
  calls: number;
  successes: number;
  failures: number;
  error_rate: number;
  average_duration_ms: number;
}

export interface ReliabilityTrendPoint {
  date: string;
  runs: number;
  success_rate: number | null;
  tokens: number;
}

export interface ReliabilityAgent {
  name: string;
  runs: number;
  success_rate: number;
  tokens: number;
  duration_p95_ms: number | null;
}

export interface ReliabilityRecentRun {
  id: string;
  session_id: string;
  session_title: string;
  agent_name: string;
  status: string;
  termination_reason: string;
  tokens: number;
  steps: number;
  duration_ms: number | null;
  started_at: string;
}

export interface ReliabilityOverview {
  window: {
    days: number;
    from: string;
    to: string;
    session_limit: number;
    runs_per_session_limit: number;
  };
  summary: {
    session_count: number;
    run_count: number;
    terminal_run_count: number;
    active_run_count: number;
    success_rate: number | null;
    total_tokens: number;
    average_tokens: number | null;
    duration_p50_ms: number | null;
    duration_p95_ms: number | null;
    tool_call_count: number;
    tool_error_rate: number | null;
  };
  status_counts: Record<string, number>;
  failure_reasons: Array<{ reason: string; count: number }>;
  tools: ReliabilityTool[];
  trend: ReliabilityTrendPoint[];
  agents: ReliabilityAgent[];
  recent_runs: ReliabilityRecentRun[];
  objectives: ReliabilityObjective[];
  coverage: {
    sessions_scanned: number;
    sessions_with_runs: number;
    terminal_runs: number;
    runs_with_duration: number;
    failed_runs_with_reason: number;
    tool_steps: number;
  };
  disclosure: {
    source: string;
    currency_cost_available: boolean;
    token_usage_is_not_currency_cost: boolean;
    reference_objectives_are_not_production_slas: boolean;
    zero_token_legacy_runs_are_preserved: boolean;
  };
}
