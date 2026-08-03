import { apiGet, apiPost, apiDelete, apiPatch } from "./client";
import type {
  SessionSummary,
  SessionDetail,
  Message,
  EventsResponse,
  WsMessage,
  TimelineResponse,
} from "../types";
import type { RunEvidenceRecord } from "../types/events";

export function listSessions(limit = 50): Promise<SessionSummary[]> {
  return apiGet(`/api/sessions?limit=${limit}`);
}

export function getSession(id: string): Promise<SessionDetail> {
  return apiGet(`/api/sessions/${encodeURIComponent(id)}`);
}

export function getMessages(id: string, signal?: AbortSignal): Promise<Message[]> {
  return apiGet(`/api/sessions/${encodeURIComponent(id)}/messages`, signal);
}

export function getEvents(
  id: string,
  after = 0,
  limit = 100,
  signal?: AbortSignal,
): Promise<EventsResponse> {
  return apiGet(
    `/api/sessions/${encodeURIComponent(id)}/events?after=${after}&limit=${limit}`, signal,
  );
}

export function getTraceEvents(
  id: string,
  after = 0,
  limit = 200,
  signal?: AbortSignal,
  afterSeq = 0,
): Promise<WsMessage[]> {
  const seq = afterSeq > 0 ? `&after_seq=${afterSeq}` : "";
  return apiGet(
    `/api/sessions/${encodeURIComponent(id)}/trace/events?after=${after}&limit=${limit}${seq}`, signal,
  );
}

export function getTimeline(
  id: string,
  signal?: AbortSignal,
  afterSeq = 0,
  limit = 200,
): Promise<TimelineResponse> {
  const seq = afterSeq > 0 ? `&after_seq=${afterSeq}` : "";
  return apiGet(
    `/api/sessions/${encodeURIComponent(id)}/timeline?limit=${limit}${seq}`, signal,
  );
}

export function getRunEvidence(
  sessionId: string,
  runId: string,
  signal?: AbortSignal,
): Promise<{
  run_id: string;
  schema_version: number;
  evidence: RunEvidenceRecord[];
}> {
  return apiGet(
    `/api/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/evidence`,
    signal,
  );
}

export function createSession(
  agentName: string,
  repoPath: string,
  title?: string,
  initialPlanFile?: string
): Promise<{ session_id: string }> {
  return apiPost("/api/sessions", {
    agent_name: agentName,
    repo_path: repoPath,
    title: title || `Session ${new Date().toLocaleTimeString()}`,
    ...(initialPlanFile ? { initial_plan_file: initialPlanFile } : {}),
  });
}

export function chat(
  sessionId: string,
  prompt: string,
  intent?: string,
  agentName?: string,
  idempotencyKey?: string,
  skill?: { name: string; arguments?: string },
  productMode?: string,
): Promise<Record<string, unknown>> {
  const body: Record<string, unknown> = { prompt };
  if (intent) body.intent = intent;
  if (agentName) body.agent_name = agentName;
  if (idempotencyKey) body.idempotency_key = idempotencyKey;
  if (productMode) body.product_mode = productMode;
  if (skill) {
    body.skill_name = skill.name;
    body.skill_arguments = skill.arguments || "";
  }
  return apiPost(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, body);
}

export function updateSession(
  sessionId: string,
  data: { agent_name?: string; title?: string },
): Promise<{ updated: boolean; agent_name: string | null; title?: string }> {
  return apiPatch(`/api/sessions/${encodeURIComponent(sessionId)}`, data);
}

export function updateSessionModel(
  sessionId: string,
  data: { model: string; provider?: string },
): Promise<{ updated?: boolean; model?: string | null; provider?: string | null }> {
  return apiPost(`/api/sessions/${encodeURIComponent(sessionId)}/model`, {
    model: data.model,
    provider: data.provider || "",
  });
}

export function compactSession(
  sessionId: string,
): Promise<{ accepted: boolean }> {
  return apiPost(`/api/sessions/${encodeURIComponent(sessionId)}/compact`);
}

export function deleteSession(
  sessionId: string
): Promise<{ deleted: boolean }> {
  return apiDelete(`/api/sessions/${encodeURIComponent(sessionId)}`);
}

export function deleteSessionsBatch(
  sessionIds: string[]
): Promise<{ deleted_count: number; total_requested: number }> {
  return apiPost("/api/sessions/batch-delete", { session_ids: sessionIds });
}

export function cancelSession(
  sessionId: string,
  detail?: string
): Promise<{ cancelled: boolean }> {
  return apiPost(
    `/api/sessions/${encodeURIComponent(sessionId)}/cancel`,
    { detail: detail || "" }
  );
}

export function approveSession(
  sessionId: string,
  comment?: string
): Promise<{ approved: boolean }> {
  return apiPost(`/api/sessions/${encodeURIComponent(sessionId)}/approve`, {
    comment: comment || "",
  });
}

export function rejectSession(
  sessionId: string,
  reason: string
): Promise<{ approved: boolean }> {
  return apiPost(`/api/sessions/${encodeURIComponent(sessionId)}/reject`, {
    reason,
  });
}

export function savePlan(
  sessionId: string,
): Promise<{ saved: boolean }> {
  return apiPost(`/api/sessions/${encodeURIComponent(sessionId)}/save-plan`);
}

export function abortPlan(
  sessionId: string,
): Promise<{ aborted: boolean }> {
  return apiPost(`/api/sessions/${encodeURIComponent(sessionId)}/abort-plan`);
}

export function resolveWorktree(
  sessionId: string,
  childId: string,
  action: string,
  expectedRevision: string,
): Promise<{ accepted: boolean; command_key: string; child_session_id: string; action: string; status: string }> {
  return apiPost(
    `/api/sessions/${encodeURIComponent(sessionId)}/worktrees/${encodeURIComponent(childId)}/${encodeURIComponent(action)}`,
    { expected_revision: expectedRevision },
  );
}

export function resolveToolApproval(
  sessionId: string,
  data: {
    request_id: string;
    decision: "allow" | "deny";
    note?: string;
    always?: boolean;
  },
): Promise<{ approved?: boolean; accepted?: boolean }> {
  return apiPost(`/api/sessions/${encodeURIComponent(sessionId)}/tool-approve`, {
    request_id: data.request_id,
    decision: data.decision,
    note: data.note || "",
    always: data.always || false,
  });
}

export interface SkillInfo {
  name: string;
  display_name: string;
  description: string;
  user_invocable: boolean;
}

export function fetchSkills(signal?: AbortSignal): Promise<SkillInfo[]> {
  return apiGet("/api/skills", signal);
}

export interface SessionTreeNode {
  id: string;
  agent_name: string;
  title: string;
  status: string;
  depth: number;
  parent_id: string | null;
  created_at: string;
  children: SessionTreeNode[];
  child_count: number;
}

export function fetchSessionTree(id: string): Promise<SessionTreeNode> {
  return apiGet(`/api/sessions/${encodeURIComponent(id)}/tree`);
}
