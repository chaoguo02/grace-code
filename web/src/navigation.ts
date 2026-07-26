export type ModuleName =
  | "overview"
  | "workbench"
  | "inspect"
  | "control"
  | "quality";

export type ScopeName = "project" | "session" | "run" | "hybrid";

export type ViewName =
  | "overview"
  | "chat"
  | "plans"
  | "reviews"
  | "memory"
  | "runs"
  | "context"
  | "replay"
  | "events"
  | "architecture"
  | "agents"
  | "safety"
  | "reliability"
  | "evaluations";

export interface NavigationState {
  module: ModuleName;
  view: ViewName;
  runId?: string;
  turnId?: string;
  sequence?: number;
}

export type NavigationTarget = Pick<
  NavigationState,
  "runId" | "turnId" | "sequence"
>;

export interface ViewDefinition {
  key: ViewName;
  label: string;
  level: "core" | "standard" | "advanced" | "expert";
  scope: ScopeName;
}

export interface ModuleDefinition {
  key: ModuleName;
  label: string;
  icon: string;
  defaultView: ViewName;
  views: readonly ViewDefinition[];
}

export const MODULES: readonly ModuleDefinition[] = [
  {
    key: "overview",
    label: "Overview",
    icon: "O",
    defaultView: "overview",
    views: [{ key: "overview", label: "Overview", level: "core", scope: "project" }],
  },
  {
    key: "workbench",
    label: "Workbench",
    icon: "W",
    defaultView: "chat",
    views: [
      { key: "chat", label: "Chat", level: "core", scope: "session" },
      { key: "plans", label: "Plans", level: "standard", scope: "hybrid" },
      { key: "reviews", label: "Changes", level: "core", scope: "hybrid" },
      { key: "memory", label: "Memory", level: "advanced", scope: "hybrid" },
    ],
  },
  {
    key: "inspect",
    label: "Inspect",
    icon: "I",
    defaultView: "runs",
    views: [
      { key: "runs", label: "Run Summary", level: "core", scope: "session" },
      { key: "context", label: "Context", level: "advanced", scope: "session" },
      { key: "replay", label: "Replay", level: "advanced", scope: "session" },
      { key: "events", label: "Event Trace", level: "expert", scope: "session" },
    ],
  },
  {
    key: "control",
    label: "Control",
    icon: "C",
    defaultView: "architecture",
    views: [
      { key: "architecture", label: "Architecture", level: "standard", scope: "hybrid" },
      { key: "agents", label: "Agents", level: "core", scope: "session" },
      { key: "safety", label: "Safety", level: "core", scope: "hybrid" },
    ],
  },
  {
    key: "quality",
    label: "Quality",
    icon: "Q",
    defaultView: "reliability",
    views: [
      { key: "reliability", label: "Health", level: "core", scope: "project" },
      { key: "evaluations", label: "Evaluations", level: "advanced", scope: "project" },
    ],
  },
] as const;

const MODULE_BY_NAME = new Map(
  MODULES.map((module) => [module.key, module]),
);

const MODULE_BY_VIEW = new Map<ViewName, ModuleName>(
  MODULES.flatMap((module) => (
    module.views.map((view) => [view.key, module.key] as const)
  )),
);

const VIEW_ALIASES: Record<string, ViewName> = {
  eval: "evaluations",
  evaluation: "evaluations",
  health: "reliability",
  review: "reviews",
  changes: "reviews",
  system: "architecture",
  trace: "events",
  run: "runs",
};

export const DEFAULT_NAVIGATION: NavigationState = {
  module: "overview",
  view: "overview",
};

export function isModuleName(value: string): value is ModuleName {
  return MODULE_BY_NAME.has(value as ModuleName);
}

export function normalizeViewName(value: string): ViewName | null {
  const normalized = VIEW_ALIASES[value] || value;
  return MODULE_BY_VIEW.has(normalized as ViewName)
    ? normalized as ViewName
    : null;
}

export function navigationForView(
  view: ViewName,
  target: NavigationTarget = {},
): NavigationState {
  return {
    module: MODULE_BY_VIEW.get(view) || "overview",
    view,
    ...target,
  };
}

export function defaultNavigationForModule(
  moduleName: ModuleName,
): NavigationState {
  const module = MODULE_BY_NAME.get(moduleName);
  return {
    module: moduleName,
    view: module?.defaultView || "overview",
  };
}

export function parseNavigation(search: string): NavigationState {
  const params = new URLSearchParams(search);
  const view = normalizeViewName(params.get("view") || "");
  if (view) {
    const sequenceValue = Number(params.get("sequence"));
    return navigationForView(view, {
      runId: params.get("run")?.trim() || undefined,
      turnId: params.get("turn")?.trim() || undefined,
      sequence: Number.isSafeInteger(sequenceValue) && sequenceValue > 0
        ? sequenceValue
        : undefined,
    });
  }
  const moduleName = params.get("module") || "";
  return isModuleName(moduleName)
    ? defaultNavigationForModule(moduleName)
    : DEFAULT_NAVIGATION;
}

export function parseNavigationSession(search: string): string {
  return new URLSearchParams(search).get("session")?.trim() || "";
}

export function buildNavigationSearch(
  currentSearch: string,
  navigation: NavigationState,
  sessionId?: string | null,
): string {
  const params = new URLSearchParams(currentSearch);
  params.set("module", navigation.module);
  params.set("view", navigation.view);
  if (navigation.runId) params.set("run", navigation.runId);
  else params.delete("run");
  if (navigation.turnId) params.set("turn", navigation.turnId);
  else params.delete("turn");
  if (navigation.sequence) params.set("sequence", String(navigation.sequence));
  else params.delete("sequence");
  if (sessionId) params.set("session", sessionId);
  else params.delete("session");
  const serialized = params.toString();
  return serialized ? `?${serialized}` : "";
}

export function moduleDefinition(moduleName: ModuleName): ModuleDefinition {
  return MODULE_BY_NAME.get(moduleName) || MODULES[0];
}

export function viewDefinition(viewName: ViewName): ViewDefinition {
  const moduleName = MODULE_BY_VIEW.get(viewName) || "overview";
  return moduleDefinition(moduleName).views.find((view) => view.key === viewName)
    || MODULES[0].views[0];
}

export function navigationScope(navigation: NavigationState): ScopeName {
  if (navigation.runId) return "run";
  return viewDefinition(navigation.view).scope;
}
