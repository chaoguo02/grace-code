import { apiGet } from "./client";

export interface AgentInfo {
  name: string;
  description: string;
  intent: string;
  tools: string[];
  max_turns: number;
}

export function getAgents(): Promise<AgentInfo[]> {
  return apiGet("/api/config/agents");
}
