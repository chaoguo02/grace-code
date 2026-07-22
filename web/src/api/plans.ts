import { apiGet, apiPatch, apiDelete } from "./client";

/** Plan summary returned by list endpoint — no full content. */
export interface PlanEntry {
  filename: string;
  session_id: string | null;
  title: string;
  preview: string;
  size_bytes: number;
  created_at: string;
  session: {
    id: string;
    agent_name: string;
    title: string;
    status: string;
  } | null;
}

/** Plan detail returned by get endpoint — includes full content. */
export interface PlanDetail extends PlanEntry {
  content: string;
}

export interface PlanListResponse {
  plans: PlanEntry[];
  total: number;
  has_more: boolean;
}

export function listPlans(limit = 50, offset = 0): Promise<PlanListResponse> {
  return apiGet(`/api/plans?limit=${limit}&offset=${offset}`);
}

export function getPlan(filename: string): Promise<PlanDetail> {
  return apiGet(`/api/plans/${encodeURIComponent(filename)}`);
}

export function updatePlan(filename: string, content: string): Promise<{ filename: string; updated: boolean; size_bytes: number }> {
  return apiPatch(`/api/plans/${encodeURIComponent(filename)}`, { content });
}

export function deletePlan(filename: string): Promise<{ filename: string; deleted: boolean }> {
  return apiDelete(`/api/plans/${encodeURIComponent(filename)}`);
}
