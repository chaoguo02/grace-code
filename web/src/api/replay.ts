import { apiGet } from "./client";
import type { SessionReplay } from "../types/replay";

export function getSessionReplay(
  sessionId: string,
  signal?: AbortSignal,
): Promise<SessionReplay> {
  return apiGet(`/api/replay/${encodeURIComponent(sessionId)}`, signal);
}
