import { describe, expect, it } from "vitest";
import { agentNameForUiMode, uiModeForAgentName } from "./modes";

describe("mode mappings", () => {
  it("keeps research and legacy explore sessions aligned", () => {
    expect(agentNameForUiMode("explore")).toBe("research");
    expect(uiModeForAgentName("research")).toBe("explore");
    expect(uiModeForAgentName("explore")).toBe("explore");
  });
});
