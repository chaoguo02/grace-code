export interface SafetyLayer {
  order: string;
  id: string;
  label: string;
  authority: string;
  can_allow: boolean;
  can_deny: boolean;
  detail: string;
}

export interface SafetyRule {
  raw: string;
  tool_name: string;
  pattern: string | null;
  tier: "deny" | "ask" | "allow" | string;
  source: string;
  source_priority: number;
}

export interface SafetyTool {
  name: string;
  risk: "critical" | "high" | "medium" | "low" | "unknown" | string;
  control: string;
  effects: string[];
  path_access: string;
  path_parameter: string;
  requires_user_interaction: boolean;
  required_permissions: string[];
  matching_rules: Array<{ raw: string; tier: string; source: string }>;
}

export interface ApprovalAuditItem {
  request_id: string;
  tool_name: string;
  decision_reason: string;
  permission_mode: string;
  risk_level: string;
  params_keys: string[];
  target: string;
  status: "resolved" | "timed_out" | "response_not_recorded" | string;
  decision: string;
  note: string;
  updated_input: boolean;
  wait_ms: number;
  requested_at: string;
  resolved_at: string;
  sequence: number;
}

export interface SafetySnapshot {
  layers: SafetyLayer[];
  rules: SafetyRule[];
  rule_summary: {
    total: number;
    by_tier: Record<string, number>;
    by_source: Record<string, number>;
    precedence: string[];
    source_priority: Record<string, number>;
  };
  tools: SafetyTool[];
  modes: Array<{ name: string; posture: string; detail: string }>;
  session: {
    session_id: string;
    agent_name: string;
    agent_kind: string;
    default_mode: string;
    pending_mode: string;
    effective_next_mode: string;
    project_root: string;
    parent_session_id: string | null;
    parent_agent_name: string;
    deny_rules_inherited: boolean;
    pending_approval_count: number;
    approvals: ApprovalAuditItem[];
    approval_summary: {
      total: number;
      allowed: number;
      denied: number;
      timed_out: number;
      response_not_recorded: number;
      average_wait_ms: number;
    };
    trace_truncated: boolean;
  } | null;
  invariants: Array<{ name: string; detail: string }>;
  disclosure: {
    source: string;
    tool_calls_executed: boolean;
    rule_simulation_performed: boolean;
    historical_responses_may_be_unrecorded: boolean;
  };
}
