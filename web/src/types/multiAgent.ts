export interface MultiAgentNode {
  id: string;
  parent_id: string | null;
  agent_name: string;
  title: string;
  status: string;
  agent_kind: string;
  context_origin: string;
  execution_placement: string;
  workspace_mode: string;
  depth: number;
  generation: number;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  selected: boolean;
  result_status: string | null;
}

export interface MultiAgentEdge {
  source: string;
  target: string;
  kind: string;
  context_origin: string;
  execution_placement: string;
  workspace_mode: string;
}

export interface AgentCommunication {
  id: string;
  kind: "delegation" | "completion" | string;
  source_session_id: string | null;
  target_session_id: string;
  generation: number;
  created_at: string;
  delivered_at?: string | null;
  delivery_state: string;
  status: string;
  summary: string;
  source: string;
}

export interface AgentContextProjection {
  session_id: string;
  agent_name: string;
  origin: string;
  generation: number;
  message_count: number;
  token_estimate: number;
  isolation_boundary: string;
  tool_contract_persisted: boolean;
}

export interface AgentWorktreeProjection {
  session_id: string;
  parent_session_id: string | null;
  agent_name: string;
  disposition: string;
  consistency_state: string;
  change: string;
  changed_files: string[];
  branch: string;
  base_branch: string;
  revision: string;
}

export interface ConsistencyCheck {
  id: string;
  label: string;
  passed: boolean;
  detail: string;
}

export interface AgentRoutingProjection {
  topology: string;
  reason_code: string;
  explanation: string;
  downgraded_from?: string | null;
}

export interface AgentBudgetProjection {
  token_limit?: number | null;
  tokens_used?: number;
  time_limit_ms?: number | null;
  elapsed_ms?: number;
  spawn_limit?: number | null;
  spawns_used?: number;
  concurrent_limit?: number | null;
  active_workers?: number;
  exhausted?: boolean;
  reason?: string | null;
  max_spawn_per_session?: number | null;
  max_concurrent_subagents?: number | null;
  max_subagent_spawn_depth?: number | null;
  max_fanout_per_turn?: number | null;
}

export interface DelegationRunProjection {
  id: string;
  topology: string;
  reason?: string;
  status: string;
  required_count?: number;
  completed_count?: number;
  failed_count?: number;
  retry_count?: number;
  budget?: AgentBudgetProjection;
}

export interface DelegationTaskProjection {
  id: string;
  run_id?: string;
  title: string;
  description?: string;
  agent_name: string;
  child_session_id?: string | null;
  status: string;
  required: boolean;
  dependencies: string[];
  generation?: number;
  retry_count?: number;
  max_retries?: number;
  failure_category?: string | null;
  failure_detail?: string | null;
  evidence_status?: string | null;
  token_budget?: number | null;
  tokens_used?: number;
  time_budget_ms?: number | null;
  elapsed_ms?: number;
}

export interface AgentTeamProjection {
  id?: string;
  enabled: boolean;
  available: boolean;
  active: boolean;
  state?: string;
  approval_required: boolean;
  arbitrary_agent_message_bus: boolean;
  shared_task_board: boolean;
  direct_messaging: boolean;
  reason?: string;
  recovery_note?: string;
  members: Array<{
    id: string;
    role: string;
    state: string;
  }>;
  task_board: Array<{
    id: string;
    goal: string;
    dependencies: string[];
    status: string;
    assignee_id: string;
    result_summary: string;
  }>;
  mailbox?: {
    pending: number;
    persisted?: boolean;
  };
}

export interface MultiAgentSnapshot {
  selected_session_id: string;
  root_session_id: string;
  nodes: MultiAgentNode[];
  edges: MultiAgentEdge[];
  scheduler: {
    total_agents: number;
    active_agents: number;
    terminal_agents: number;
    peak_observed_parallelism: number;
    status_counts: Record<string, number>;
    placement_counts: Record<string, number>;
    max_depth: number;
  };
  communications: AgentCommunication[];
  communication_summary: {
    delegations: number;
    completion_notifications: number;
    pending_delivery: number;
    delivered: number;
  };
  contexts: AgentContextProjection[];
  worktrees: AgentWorktreeProjection[];
  consistency: {
    state: string;
    unresolved_worktrees: number;
    checks: ConsistencyCheck[];
  };
  invariants: Array<{ name: string; detail: string }>;
  disclosure: {
    source: string;
    arbitrary_agent_message_bus: boolean;
    scheduler_simulation_performed: boolean;
    parallelism_is_interval_projection: boolean;
  };
  routing?: AgentRoutingProjection | null;
  /** Compatibility with early routing-decision API drafts. */
  routing_decision?: AgentRoutingProjection | null;
  delegation_runs?: DelegationRunProjection[];
  delegation_tasks?: DelegationTaskProjection[];
  limits?: AgentBudgetProjection | null;
  team?: AgentTeamProjection | null;
}
