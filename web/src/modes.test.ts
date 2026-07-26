import { agentNameForUiMode, uiModeForAgentName } from "./modes";

if (agentNameForUiMode("explore") !== "research") {
  throw new Error("Explore must enter through the research primary");
}
if (uiModeForAgentName("research") !== "explore") {
  throw new Error("Persisted research sessions must reopen on the Explore tab");
}
if (uiModeForAgentName("explore") !== "explore") {
  throw new Error("Legacy explore sessions must remain readable");
}
