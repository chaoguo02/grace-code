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
    expect(MODULES).toHaveLength(4);
    const allViews = MODULES.flatMap((module) => module.views);
    expect(allViews).toHaveLength(10);
    expect(new Set(allViews.map((view) => view.key))).toHaveLength(10);
    expect(navigationForView("chat").module).toBe("workbench");
    expect(navigationForView("plans").module).toBe("workbench");
    expect(navigationForView("reviews").module).toBe("changes");
    expect(navigationForView("memory").module).toBe("history");
    expect(navigationForView("runs").module).toBe("history");
    expect(navigationForView("context").module).toBe("history");
    expect(navigationForView("overview").module).toBe("system");
    expect(navigationForView("agents").module).toBe("system");
    expect(navigationForView("safety").module).toBe("system");
    expect(navigationForView("evaluations").module).toBe("system");
    expect(navigationScope(navigationForView("overview"))).toBe("project");
    expect(navigationScope(navigationForView("agents"))).toBe("session");
    expect(navigationScope(navigationForView("plans"))).toBe("hybrid");
    expect(navigationScope(navigationForView("context", { runId: "run-1" }))).toBe("run");
  });

  it("keeps aliases and URL identity compatible", () => {
    expect(parseNavigation("?view=review").view).toBe("reviews");
    expect(parseNavigation("?view=changes").view).toBe("reviews");
    expect(parseNavigation("?view=run").view).toBe("runs");
    // Legacy module URLs that still resolve to surviving views keep working.
    expect(parseNavigation("?module=overview").view).toBe("overview");
    expect(parseNavigation("?module=inspect").view).toBe("runs");
    expect(defaultNavigationForModule("history").view).toBe("memory");
    const search = buildNavigationSearch(
      "?unrelated=kept",
      { module: "history", view: "runs", runId: "run-1", turnId: "turn-1", sequence: 42 },
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

  it("drops URLs for deleted views back to the default navigation", () => {
    // Views removed in the page reduction no longer resolve to a route.
    expect(parseNavigation("?view=architecture").view).toBe("chat");
    expect(parseNavigation("?view=replay").view).toBe("chat");
    expect(parseNavigation("?view=reliability").view).toBe("chat");
    expect(parseNavigation("?view=events").view).toBe("chat");
    expect(parseNavigation("?view=trace").view).toBe("chat");
  });
});
