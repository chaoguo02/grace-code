import { describe, expect, it } from "vitest";
import { groupComponentsByLayer } from "./ArchitectureExplorer";

describe("groupComponentsByLayer", () => {
  it("uses the canonical layer order", () => {
    const groups = groupComponentsByLayer([
      { id: "runtime", label: "Runtime", layer: "orchestration", status: "available", responsibility: "Runs sessions" },
      { id: "web", label: "Web", layer: "interface", status: "available", responsibility: "Presents facts" },
    ]);
    expect(groups.map((group) => group.key)).toEqual(["interface", "orchestration"]);
  });
});
