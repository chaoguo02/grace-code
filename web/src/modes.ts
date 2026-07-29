export type UiMode = "build" | "plan" | "multi-agent";

const UI_TO_AGENT: Record<UiMode, string> = {
  build: "build",
  plan: "plan",
  "multi-agent": "orchestrator",
};

/**
 * UI modes are product labels; backend agent names are runtime identities.
 * Multi-Agent is orchestrated by the orchestrator primary.
 */
export function agentNameForUiMode(mode: string): string {
  return UI_TO_AGENT[mode as UiMode] || mode;
}

export function uiModeForAgentName(agentName: string | null | undefined): UiMode | null {
  if (agentName === "orchestrator" || agentName === "research" || agentName === "explore") {
    return "multi-agent";
  }
  if (agentName === "build" || agentName === "plan") return agentName;
  return null;
}
