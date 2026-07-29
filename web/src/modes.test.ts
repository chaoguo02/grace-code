import { describe, expect, it } from "vitest";
import { agentNameForUiMode, uiModeForAgentName } from "./modes";

describe("mode mappings", () => {
  it("keeps build, plan, and multi-agent sessions aligned", () => {
    expect(agentNameForUiMode("build")).toBe("build");
    expect(agentNameForUiMode("plan")).toBe("plan");
    expect(agentNameForUiMode("multi-agent")).toBe("orchestrator");
    expect(uiModeForAgentName("orchestrator")).toBe("multi-agent");
    expect(uiModeForAgentName("research")).toBe("multi-agent");
    expect(uiModeForAgentName("explore")).toBe("multi-agent");
  });
});
