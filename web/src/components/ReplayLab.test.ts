import { describe, expect, it } from "vitest";
import { deriveVisibleToolDelta } from "./ReplayLab";

describe("deriveVisibleToolDelta", () => {
  it("reports added, removed, and stable tools", () => {
    const delta = deriveVisibleToolDelta(
      [{ name: "Read", visible: true }, { name: "Edit", visible: true }],
      [{ name: "Read", visible: true }, { name: "Bash", visible: true }],
    );
    expect(delta.added).toEqual(["Edit"]);
    expect(delta.removed).toEqual(["Bash"]);
    expect(delta.unchanged).toEqual(["Read"]);
  });
});
