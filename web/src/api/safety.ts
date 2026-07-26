import { apiGet } from "./client";
import type { SafetySnapshot } from "../types/safety";

export function getSafetySnapshot(
  sessionId?: string | null,
  signal?: AbortSignal,
): Promise<SafetySnapshot> {
  const query = sessionId
    ? `?session_id=${encodeURIComponent(sessionId)}`
    : "";
  return apiGet(`/api/safety${query}`, signal);
}
