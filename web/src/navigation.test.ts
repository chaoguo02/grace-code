import { describe, expect, it } from "vitest";
import {
  MODULES,
  buildNavigationSearch,
  defaultNavigationForModule,
  navigationScope,
  navigationForView,
  parseNavigation,
  parseNavigationSession,
} from "./navigation";

describe("navigation contract", () => {
  it("maps every view to its canonical module and scope", () => {
    expect(MODULES).toHaveLength(5);
    const allViews = MODULES.flatMap((module) => module.views);
    expect(allViews).toHaveLength(14);
    expect(new Set(allViews.map((view) => view.key))).toHaveLength(14);
    expect(navigationForView("events").module).toBe("inspect");
    expect(navigationForView("memory").module).toBe("workbench");
    expect(navigationForView("agents").module).toBe("control");
    expect(navigationForView("safety").module).toBe("control");
    expect(navigationForView("reliability").module).toBe("quality");
    expect(navigationForView("evaluations").module).toBe("quality");
    expect(navigationScope(navigationForView("reliability"))).toBe("project");
    expect(navigationScope(navigationForView("agents"))).toBe("session");
    expect(navigationScope(navigationForView("architecture"))).toBe("hybrid");
    expect(navigationScope(navigationForView("context", { runId: "run-1" }))).toBe("run");
  });

  it("keeps aliases and URL identity compatible", () => {
    expect(parseNavigation("?view=review").view).toBe("reviews");
    expect(parseNavigation("?module=quality").view).toBe("reliability");
    expect(defaultNavigationForModule("control").view).toBe("architecture");
    const search = buildNavigationSearch(
      "?unrelated=kept",
      { module: "inspect", view: "runs", runId: "run-1", turnId: "turn-1", sequence: 42 },
      "session-1",
    );
    expect(parseNavigation(search)).toMatchObject({
      view: "runs", runId: "run-1", turnId: "turn-1", sequence: 42,
    });
    expect(parseNavigationSession(search)).toBe("session-1");
    expect(search).toContain("unrelated=kept");
    const clearedTarget = buildNavigationSearch(search, navigationForView("chat"), "session-1");
    expect(clearedTarget).not.toMatch(/(?:run|turn|sequence)=/);
  });
});
