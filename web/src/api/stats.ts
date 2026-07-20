import { apiGet } from "./client";
import type { SessionStats, DailyRollup } from "../types/stats";

export function getSessionStats(id: string): Promise<SessionStats> {
  return apiGet(`/api/sessions/${encodeURIComponent(id)}/stats`);
}

export function getSessionSteps(id: string): Promise<any[]> {
  return apiGet(`/api/sessions/${encodeURIComponent(id)}/steps`);
}

export function getDailyRollups(days = 30): Promise<DailyRollup[]> {
  return apiGet(`/api/stats/daily?days=${days}`);
}

export function getToolRankings(days = 7): Promise<Record<string, number>> {
  return apiGet(`/api/stats/tools?days=${days}`);
}

export function getRecentSessionStats(days = 30): Promise<SessionStats[]> {
  return apiGet(`/api/stats/sessions?days=${days}`);
}
