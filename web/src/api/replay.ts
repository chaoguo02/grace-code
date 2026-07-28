import { apiDelete, apiGet, apiPost } from "./client";
import type { ReplayExecution, SessionReplay } from "../types/replay";

export function getSessionReplay(
  sessionId: string,
  signal?: AbortSignal,
): Promise<SessionReplay> {
  return apiGet(`/api/replay/${encodeURIComponent(sessionId)}`, signal);
}

export function startReplayExecution(
  sessionId: string,
  runId: string,
): Promise<ReplayExecution> {
  return apiPost(
    `/api/replay/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/executions`,
  );
}

export function getReplayExecution(
  executionId: string,
  signal?: AbortSignal,
): Promise<ReplayExecution> {
  return apiGet(`/api/replay/executions/${encodeURIComponent(executionId)}`, signal);
}

export function pinReplayExecution(executionId: string): Promise<ReplayExecution> {
  return apiPost(`/api/replay/executions/${encodeURIComponent(executionId)}/pin`);
}

export function deleteReplayWorkspace(executionId: string): Promise<ReplayExecution> {
  return apiDelete(`/api/replay/executions/${encodeURIComponent(executionId)}/workspace`);
}
