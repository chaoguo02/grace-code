import { apiGet, apiPatch } from "./client";
import type { SessionDiff } from "../types/stats";

export function getSessionDiffs(
  sessionId: string,
  status?: string,
  signal?: AbortSignal,
): Promise<SessionDiff[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  return apiGet(`/api/sessions/${encodeURIComponent(sessionId)}/diffs${query}`, signal);
}

export function getPendingDiffs(): Promise<SessionDiff[]> {
  return apiGet("/api/diffs/pending");
}

export function updateDiffStatus(
  diffId: number,
  status: "approved" | "rejected",
  comment = "",
): Promise<{ updated: boolean; status: string }> {
  return apiPatch(`/api/diffs/${diffId}`, { status, comment });
}
