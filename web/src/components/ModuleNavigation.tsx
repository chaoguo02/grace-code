import type { KeyboardEvent } from "react";

import {
  MODULES,
  moduleDefinition,
  navigationScope,
  viewDefinition,
  type ModuleName,
  type NavigationState,
  type ScopeName,
  type ViewName,
} from "../navigation";

const SCOPE_LABELS: Record<ScopeName, string> = {
  project: "Project scope",
  session: "Session scope",
  run: "Run scope",
  hybrid: "Project + session",
};

function moveNavigationFocus(event: KeyboardEvent<HTMLButtonElement>) {
  if (event.altKey || event.ctrlKey || event.metaKey) return;
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const parent = event.currentTarget.parentElement;
  if (!parent) return;
  const buttons = Array.from(
    parent.querySelectorAll<HTMLButtonElement>(":scope > button:not(:disabled)"),
  );
  const currentIndex = buttons.indexOf(event.currentTarget);
  if (currentIndex < 0 || buttons.length < 2) return;
  const nextIndex = event.key === "Home"
    ? 0
    : event.key === "End"
      ? buttons.length - 1
      : event.key === "ArrowRight"
        ? (currentIndex + 1) % buttons.length
        : (currentIndex - 1 + buttons.length) % buttons.length;
  event.preventDefault();
  buttons[nextIndex].focus();
  buttons[nextIndex].click();
}

export function PrimaryNavigation({
  activeModule,
  onSelect,
}: {
  activeModule: ModuleName;
  onSelect: (module: ModuleName) => void;
}) {
  return (
    <nav className="primary-navigation" aria-label="Primary modules">
      {MODULES.map((module) => (
        <button
          type="button"
          key={module.key}
          className={activeModule === module.key ? "active" : ""}
          aria-current={activeModule === module.key ? "page" : undefined}
          tabIndex={activeModule === module.key ? 0 : -1}
          onKeyDown={moveNavigationFocus}
          onClick={() => onSelect(module.key)}
        >
          <span aria-hidden="true">{module.icon}</span>
          {module.label}
        </button>
      ))}
    </nav>
  );
}

export function SecondaryNavigation({
  navigation,
  onSelect,
}: {
  navigation: NavigationState;
  onSelect: (view: ViewName) => void;
}) {
  const module = moduleDefinition(navigation.module);
  if (module.views.length <= 1) return null;
  return (
    <nav className="secondary-navigation" aria-label={`${module.label} views`}>
      <span className="secondary-navigation-label" aria-hidden="true">
        {module.label}
      </span>
      <div role="tablist" aria-orientation="horizontal">
        {module.views.map((view) => (
          <button
            type="button"
            role="tab"
            key={view.key}
            className={`view-tab${navigation.view === view.key ? " active" : ""}`}
            data-view={view.key}
            aria-selected={navigation.view === view.key}
            tabIndex={navigation.view === view.key ? 0 : -1}
            data-level={view.level}
            onKeyDown={moveNavigationFocus}
            onClick={() => onSelect(view.key)}
          >
            {view.label}
            {view.level === "expert" && <small>Expert</small>}
          </button>
        ))}
      </div>
    </nav>
  );
}

export function ViewScope({ navigation }: { navigation: NavigationState }) {
  const scope = navigationScope(navigation);
  const view = viewDefinition(navigation.view);
  const identity = scope === "run"
    ? navigation.runId?.slice(0, 8)
    : undefined;
  return (
    <div
      className={`view-scope view-scope-${scope}`}
      title={`${view.label}: ${SCOPE_LABELS[scope]}`}
      aria-label={`${view.label}: ${SCOPE_LABELS[scope]}`}
    >
      <i aria-hidden="true" />
      <span>{SCOPE_LABELS[scope]}</span>
      {identity && <code>{identity}</code>}
    </div>
  );
}
