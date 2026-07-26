export type UiMode = "build" | "plan" | "explore";

const UI_TO_AGENT: Record<UiMode, string> = {
  build: "build",
  plan: "plan",
  explore: "research",
};

/**
 * UI modes are product labels; backend agent names are runtime identities.
 * Explore is orchestrated by the research primary; explore remains a leaf.
 */
export function agentNameForUiMode(mode: string): string {
  return UI_TO_AGENT[mode as UiMode] || mode;
}

export function uiModeForAgentName(agentName: string | null | undefined): UiMode | null {
  if (agentName === "research" || agentName === "explore") return "explore";
  if (agentName === "build" || agentName === "plan") return agentName;
  return null;
}
