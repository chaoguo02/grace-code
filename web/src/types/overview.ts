export type OverviewRoute =
  | "chat"
  | "runs"
  | "context"
  | "evaluations"
  | "agents"
  | "safety"
  | "reviews"
  | "plans"
  | "memory";

export interface OverviewCapability {
  id: string;
  label: string;
  claim: string;
  route: OverviewRoute;
  evidence_route: OverviewRoute;
  evidence_state: "observed" | "configured" | "unavailable" | string;
  evidence: string;
}

export interface OverviewRecentSession {
  id: string;
  title: string;
  agent_name: string;
  status: string;
  updated_at: string;
  message_count: number;
  selected: boolean;
}

export interface ProjectOverview {
  project: {
    name: string;
    product_name: string;
    tagline: string;
    provider: string;
    model: string;
    selected_session_id: string;
  };
  headline: {
    configured_agents: number;
    registered_tools: number;
    skills: number;
    mcp_servers: number;
    recent_sessions: number;
    persisted_runs_30d: number;
    run_success_rate: number | null;
    evaluation_pass_rate: number | null;
  };
  evidence_coverage: {
    observed: number;
    configured: number;
    unavailable: number;
    total: number;
    state: string;
  };
  capabilities: OverviewCapability[];
  recent_sessions: OverviewRecentSession[];
  signals: {
    reliability: {
      success_rate: number | null;
      duration_p95_ms: number | null;
      tool_error_rate: number | null;
      terminal_runs: number;
    };
    evaluation: {
      run_count: number;
      latest_pass_rate: number | null;
      regression_count: number;
    };
    safety: {
      layers: number;
      rules: number;
      tools: number;
      session_approvals: number;
    };
    multi_agent: {
      available_for_selected_session: boolean;
      agents: number;
      peak_parallelism: number;
      consistency: string | null;
    };
    replay: {
      available_for_selected_session: boolean;
      runs: number;
      contracts: number;
      valid: number;
    };
  };
  section_errors: Array<{ section: string; message: string }>;
  disclosure: {
    source: string;
    read_only: boolean;
    capability_is_not_runtime_success: boolean;
    missing_evidence_is_not_failure: boolean;
    sections_degrade_independently: boolean;
  };
}
