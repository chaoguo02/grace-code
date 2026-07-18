import { apiGet, apiPost, apiDelete } from "./client";
import type {
  SessionSummary,
  SessionDetail,
  Message,
  EventsResponse,
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
  prompt: string
): Promise<Record<string, unknown>> {
  return apiPost(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, {
    prompt,
  });
}

export function deleteSession(
  sessionId: string
): Promise<{ deleted: boolean }> {
  return apiDelete(`/api/sessions/${encodeURIComponent(sessionId)}`);
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
