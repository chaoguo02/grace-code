export type ArchitectureStatus = "available" | "warning" | "disabled" | string;

export interface ArchitectureComponent {
  id: string;
  label: string;
  layer: string;
  status: ArchitectureStatus;
  responsibility: string;
}

export interface ArchitectureEdge {
  source: string;
  target: string;
  label: string;
  kind: "control" | "data";
}

export interface ArchitectureAgent {
  name: string;
  description: string;
  kind: string;
  intent: string;
  visibility: string;
  workspace_mode: string;
  context_policy: string;
  permission_mode: string;
  model: string | null;
  effort: string;
  max_turns: number | null;
  max_tokens: number | null;
  background: boolean;
  memory_scope: string;
  tools: string[];
  disallowed_tools: string[];
  required_tools: string[];
  completion_requires: Record<string, unknown>;
  skills: string[];
  mcp_servers: string[];
  delegates: string[];
  source: string;
}

export interface ArchitectureTool {
  name: string;
  description: string;
  deferred: boolean;
  roles: string[];
  effects: string[];
  path_access: string;
  requires_user_interaction: boolean;
  required_permissions: string[];
  category: string;
}

export interface ArchitectureSkill {
  name: string;
  display_name: string;
  description: string;
  model_invocable: boolean;
  user_invocable: boolean;
  context: string;
  agent: string;
  model: string;
  effort: string;
  allowed_tools: string[];
  disallowed_tools: string[];
  path_scopes: string[];
}

export interface ArchitectureMcpServer {
  name: string;
  status: string;
  tools: string[];
  error: string;
}

export interface ArchitectureSessionNode {
  id: string;
  agent_name?: string;
  title?: string;
  status?: string;
  children?: ArchitectureSessionNode[];
}

export interface ArchitectureSessionOverlay {
  selected_session_id: string;
  root_session_id: string;
  agent_name: string;
  status: string;
  mode: string;
  agent_kind: string;
  context_origin: string;
  execution_placement: string;
  workspace_mode: string;
  tree: ArchitectureSessionNode | null;
  session_count: number;
  agent_names: string[];
  tool_usage: Array<{ name: string; count: number }>;
  mcp_usage: Array<{ name: string; count: number }>;
  approval_count: number;
  subagent_start_count: number;
  memory_recall_count: number;
  memory_injected_count: number;
  latest_context: Record<string, unknown> | null;
}

export interface ArchitectureSnapshot {
  components: ArchitectureComponent[];
  edges: ArchitectureEdge[];
  agents: ArchitectureAgent[];
  tools: ArchitectureTool[];
  skills: ArchitectureSkill[];
  mcp: {
    initialized: boolean;
    servers: ArchitectureMcpServer[];
    tool_names: string[];
    failed_servers: Array<{ name: string; error: string }>;
  };
  memory: {
    enabled: boolean;
    store_available: boolean;
    semantic_retrieval: boolean;
    recall_service: boolean;
    memory_count: number;
    consolidation_hook: boolean;
  };
  hitl: {
    base_approval_mode: string;
    rule_count: number;
    rules_by_tier: Record<string, number>;
    approval_broker: boolean;
    plan_approval: boolean;
    path_sandbox: boolean;
    hooks_enabled: boolean;
  };
  runtime: {
    provider: string;
    model: string;
    max_steps: number;
    execution_token_budget: number;
    request_context_budget: number;
    history_window: number;
    prompt_source: string;
    prompt_label: string;
    prompt_version: string;
    streaming_tool_execution: boolean;
  };
  session_overlay: ArchitectureSessionOverlay | null;
  disclosure: {
    configured_facts: string;
    session_facts: string;
    prompt_contents_included: boolean;
  };
}
