export interface SessionSummary {
  id: string;
  agent_name: string;
  title: string;
  status: string;
  mode: string;
  summary: string;
  error: string;
  parent_id: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  message_count?: number;
  total_tokens_estimate?: number;
}

export interface SessionDetail {
  id: string;
  parent_id: string | null;
  root_id: string | null;
  agent_name: string;
  title: string;
  status: string;
  mode: string;
  summary: string;
  error: string;
  agent_kind: string;
  context_origin: string;
  execution_placement: string;
  workspace_mode: string;
  agent_depth: number;
  generation: number;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  metadata: Record<string, unknown>;
  message_count?: number;
  total_tokens_estimate?: number;
}

export interface ToolCall {
  name: string;
  params: Record<string, unknown>;
  id?: string;
}

export interface Message {
  role: "user" | "assistant" | "tool";
  content: string;
  tool_calls?: ToolCall[];
  tool_call_id?: string | null;
}

export interface ChatResponse {
  session_id: string;
  status: string;
  summary: string;
  steps_taken: number;
  total_tokens: number;
  error: string | null;
  termination_reason: string | null;
}

export interface EventItem {
  event_id: string;
  event_type: string;
  task_id: string;
  timestamp: string;
  payload: Record<string, unknown>;
}

export interface EventsResponse {
  events: EventItem[];
  total: number;
  has_more: boolean;
}

export interface WsMessage {
  type: string;
  timestamp?: string;
  step?: number;

  // Status events
  status?: string;
  result?: {
    summary?: string;
    steps_taken?: number;
    total_tokens?: number;
  };
  error?: string;
  message?: string;

  // Thought
  content?: string;

  // Tool call
  name?: string;
  params?: Record<string, unknown>;
  id?: string;

  // Observation
  tool_name?: string;
  output?: string;
  diff?: string;           // Git diff for Edit/Write tools

  // Plan ready
  plan_text?: string;
  revision?: number;

  // Tool approval (CC control_request equivalent)
  request_id?: string;
  thought?: string;
  decision_reason?: string;
  tool_use_id?: string;
  permission_mode?: string;
  risk_level?: string;

  // Subagent
  child_session_id?: string;
  agent_name?: string;

  // Fallback raw
  payload?: Record<string, unknown>;
}

/** A rendered timeline item — either a persisted Message or a live WS event */
export type TimelineItem =
  | { source: "message"; msg: Message }
  | { source: "ws"; ws: WsMessage };
