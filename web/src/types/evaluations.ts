export interface EvaluationScenario {
  name: string;
  description: string;
  expected_status: string;
  max_steps: number;
  budget_tokens: number;
  intent: string;
  mode: string;
  expect_failure_dataset_increment: boolean;
}

export interface EvaluationResult {
  scenario: string;
  expected_status: string;
  actual_status: string;
  passed: boolean;
  repo_path: string;
  summary: string;
  steps: number;
  tokens: number;
  log_path: string;
  trace_id?: string | null;
  trace_url?: string | null;
  session_id?: string | null;
  dataset_new_entries?: number;
  details?: Record<string, unknown>;
}

export interface EvaluationConfiguration {
  provider: string;
  model: string;
  prompt_source: string;
  prompt_label: string;
  prompt_version?: number | null;
}

export interface EvaluationCheck {
  name: string;
  passed: boolean;
  details: string;
}

export interface EvaluationComparison {
  passed: boolean;
  report_path?: string | null;
  baseline_path?: string | null;
  checks: EvaluationCheck[];
  metadata: {
    current_pass_rate?: number;
    baseline_pass_rate?: number;
    current_average_tokens?: number;
    baseline_average_tokens?: number;
    max_token_regression_pct?: number;
    missing_scenarios?: string[];
    unexpected_scenarios?: string[];
    error?: string;
  };
}

export interface EvaluationRun {
  id: string;
  label: string;
  created_at: string;
  path: string;
  all_passed: boolean;
  pass_rate: number;
  passed_count: number;
  scenario_count: number;
  average_tokens: number;
  total_tokens: number;
  average_steps: number;
  results: EvaluationResult[];
  configuration: EvaluationConfiguration;
  comparison?: EvaluationComparison | null;
  comparison_source: "artifact" | "computed" | "";
}

export interface EvaluationBaseline {
  id: string;
  name: string;
  created_at: string;
  path: string;
  pass_rate: number;
  average_tokens: number;
  scenario_count: number;
  configuration: EvaluationConfiguration;
}

export interface EvaluationOverview {
  scenario_catalog: EvaluationScenario[];
  runs: EvaluationRun[];
  baselines: EvaluationBaseline[];
  domain_gates: Array<{
    domain: string;
    passed: number;
    total: number;
    completion: number;
    status: "passed" | "incomplete" | string;
    checks: Array<{ id: string; passed: boolean; evidence: string }>;
    last_success_commit: string;
    last_run_at: string;
  }>;
  summary: {
    run_count: number;
    latest_pass_rate: number;
    latest_average_tokens: number;
    pass_rate_delta?: number | null;
    token_delta_pct?: number | null;
    regression_count: number;
  };
  disclosure: {
    source: string;
    read_only: boolean;
    session_completion_is_not_a_pass: boolean;
    auto_run_enabled: boolean;
  };
}
