import { apiGet } from "./client";
import type {
  DailyRollup,
  SessionContextInspection,
  SessionStats,
  StepLog,
} from "../types/stats";

export function getSessionStats(id: string, signal?: AbortSignal): Promise<SessionStats> {
  return apiGet(`/api/sessions/${encodeURIComponent(id)}/stats`, signal);
}

export function getSessionSteps(id: string, signal?: AbortSignal): Promise<StepLog[]> {
  return apiGet(`/api/sessions/${encodeURIComponent(id)}/steps`, signal);
}

export function getSessionContext(
  id: string,
  signal?: AbortSignal,
): Promise<SessionContextInspection> {
  return apiGet(`/api/sessions/${encodeURIComponent(id)}/context`, signal);
}

export function getDailyRollups(days = 30, signal?: AbortSignal): Promise<DailyRollup[]> {
  return apiGet(`/api/stats/daily?days=${days}`, signal);
}

export function getToolRankings(days = 7, signal?: AbortSignal): Promise<Record<string, number>> {
  return apiGet(`/api/stats/tools?days=${days}`, signal);
}

export function getRecentSessionStats(days = 30, signal?: AbortSignal): Promise<SessionStats[]> {
  return apiGet(`/api/stats/sessions?days=${days}`, signal);
}
