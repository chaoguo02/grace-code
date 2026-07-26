import {
  MODULES,
  buildNavigationSearch,
  defaultNavigationForModule,
  navigationScope,
  navigationForView,
  parseNavigation,
  parseNavigationSession,
} from "./navigation";

if (MODULES.length !== 5) {
  throw new Error("The workbench must expose exactly five primary modules");
}

const allViews = MODULES.flatMap((module) => module.views);
if (allViews.length !== 14 || new Set(allViews.map((view) => view.key)).size !== 14) {
  throw new Error("Every existing view must map to exactly one module");
}
if (navigationForView("events").module !== "inspect") {
  throw new Error("Raw trace belongs to Inspect");
}
if (navigationForView("memory").module !== "workbench") {
  throw new Error("Memory belongs to the working context");
}
if (
  navigationForView("agents").module !== "control"
  || navigationForView("safety").module !== "control"
) {
  throw new Error("Agent coordination and safety policy belong to Control");
}
if (
  navigationForView("reliability").module !== "quality"
  || navigationForView("evaluations").module !== "quality"
) {
  throw new Error("Cross-session health and evaluations belong to Quality");
}
if (
  navigationScope(navigationForView("reliability")) !== "project"
  || navigationScope(navigationForView("agents")) !== "session"
  || navigationScope(navigationForView("architecture")) !== "hybrid"
) {
  throw new Error("Every view must expose an honest data scope");
}
if (
  navigationScope(navigationForView("context", { runId: "run-1" })) !== "run"
) {
  throw new Error("Run-targeted evidence must advertise run scope");
}
if (parseNavigation("?view=review").view !== "reviews") {
  throw new Error("Legacy view aliases must remain compatible");
}
if (parseNavigation("?module=quality").view !== "reliability") {
  throw new Error("A module-only URL must select its default view");
}
if (defaultNavigationForModule("control").view !== "architecture") {
  throw new Error("Control must open on configured architecture");
}

const search = buildNavigationSearch(
  "?unrelated=kept",
  {
    module: "inspect",
    view: "runs",
    runId: "run-1",
    turnId: "turn-1",
    sequence: 42,
  },
  "session-1",
);
if (
  parseNavigation(search).view !== "runs"
  || parseNavigation(search).runId !== "run-1"
  || parseNavigation(search).turnId !== "turn-1"
  || parseNavigation(search).sequence !== 42
  || parseNavigationSession(search) !== "session-1"
  || !search.includes("unrelated=kept")
) {
  throw new Error("Navigation URLs must round-trip without dropping other query state");
}

const clearedTarget = buildNavigationSearch(
  search,
  navigationForView("chat"),
  "session-1",
);
if (
  clearedTarget.includes("run=")
  || clearedTarget.includes("turn=")
  || clearedTarget.includes("sequence=")
) {
  throw new Error("Leaving an evidence view must clear stale evidence identity");
}
