import { apiGet } from "./client";
import type { EvaluationOverview } from "../types/evaluations";

export function getEvaluationOverview(
  signal?: AbortSignal,
): Promise<EvaluationOverview> {
  return apiGet("/api/evaluations", signal);
}
