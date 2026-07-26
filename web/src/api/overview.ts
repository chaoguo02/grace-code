import { apiGet } from "./client";
import type { ProjectOverview } from "../types/overview";

export function getProjectOverview(
  sessionId?: string | null,
  signal?: AbortSignal,
): Promise<ProjectOverview> {
  const query = sessionId
    ? `?session_id=${encodeURIComponent(sessionId)}`
    : "";
  return apiGet(`/api/overview${query}`, signal);
}
