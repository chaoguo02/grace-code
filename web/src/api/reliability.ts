import { apiGet } from "./client";
import type { ReliabilityOverview } from "../types/reliability";

export function getReliabilityOverview(
  days = 30,
  signal?: AbortSignal,
): Promise<ReliabilityOverview> {
  return apiGet(`/api/reliability?days=${days}`, signal);
}
