import { describe, expect, it } from "vitest";
import { evidenceLabel } from "./ProjectOverview";

describe("evidenceLabel", () => {
  it("distinguishes observed, configured, and missing evidence", () => {
    expect(evidenceLabel("observed")).toBe("Observed evidence");
    expect(evidenceLabel("configured")).toBe("Configured");
    expect(evidenceLabel("missing")).toBe("Evidence unavailable");
  });
});
