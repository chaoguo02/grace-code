import { apiGet } from "./client";
import type { ArchitectureSnapshot } from "../types/architecture";

export function getArchitectureSnapshot(
  sessionId?: string | null,
  signal?: AbortSignal,
): Promise<ArchitectureSnapshot> {
  const query = sessionId
    ? `?session_id=${encodeURIComponent(sessionId)}`
    : "";
  return apiGet(`/api/architecture${query}`, signal);
}
