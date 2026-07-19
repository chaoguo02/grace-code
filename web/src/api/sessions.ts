import { apiGet, apiPost, apiDelete, apiPatch } from "./client";
import type {
  SessionSummary,
  SessionDetail,
  Message,
  EventsResponse,
  WsMessage,
} from "../types";

export function listSessions(limit = 50): Promise<SessionSummary[]> {
  return apiGet(`/api/sessions?limit=${limit}`);
}

export function getSession(id: string): Promise<SessionDetail> {
  return apiGet(`/api/sessions/${encodeURIComponent(id)}`);
}

export function getMessages(id: string): Promise<Message[]> {
  return apiGet(`/api/sessions/${encodeURIComponent(id)}/messages`);
}

export function getEvents(
  id: string,
  after = 0,
  limit = 100
): Promise<EventsResponse> {
  return apiGet(
    `/api/sessions/${encodeURIComponent(id)}/events?after=${after}&limit=${limit}`
  );
}

export function getTraceEvents(
  id: string,
  after = 0,
  limit = 200
): Promise<WsMessage[]> {
  return apiGet(
    `/api/sessions/${encodeURIComponent(id)}/trace/events?after=${after}&limit=${limit}`
  );
}

export function createSession(
  agentName: string,
  repoPath: string,
  title?: string
): Promise<{ session_id: string }> {
  return apiPost("/api/sessions", {
    agent_name: agentName,
    repo_path: repoPath,
    title: title || `Session ${new Date().toLocaleTimeString()}`,
  });
}

export function chat(
  sessionId: string,
  prompt: string,
  intent?: string,
  agentName?: string,
): Promise<Record<string, unknown>> {
  const body: Record<string, unknown> = { prompt };
  if (intent) body.intent = intent;
  if (agentName) body.agent_name = agentName;
  return apiPost(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, body);
}

export function updateSession(
  sessionId: string,
  data: { agent_name?: string },
): Promise<{ updated: boolean; agent_name: string | null }> {
  return apiPatch(`/api/sessions/${encodeURIComponent(sessionId)}`, data);
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

export interface SkillInfo {
  name: string;
  display_name: string;
  description: string;
  user_invocable: boolean;
}

export function fetchSkills(): Promise<SkillInfo[]> {
  return apiGet("/api/skills");
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
