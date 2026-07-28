import { useCallback, useEffect, useRef, useState } from "react";
import { SessionSidebar } from "./components/SessionSidebar";
import { SessionTree } from "./components/SessionTree";
import { ChatView } from "./components/ChatView";
import { DiffReviewView } from "./components/DiffReviewView";
import { PlanLibrary } from "./components/PlanLibrary";
import { MemoryView } from "./components/MemoryView";
import { TraceView } from "./components/TraceView";
import { RunInspector } from "./components/RunInspector";
import { ContextInspector } from "./components/ContextInspector";
import { EvaluationLab } from "./components/EvaluationLab";
import { ArchitectureExplorer } from "./components/ArchitectureExplorer";
import { ReplayLab } from "./components/ReplayLab";
import { SafetyCenter } from "./components/SafetyCenter";
import { MultiAgentControlPlane } from "./components/MultiAgentControlPlane";
import { ReliabilityDashboard } from "./components/ReliabilityDashboard";
import { ProjectOverview } from "./components/ProjectOverview";
import { EventSidebar } from "./components/EventSidebar";
import { ThemeToggle } from "./components/ThemeToggle";
import { ErrorBoundary } from "./components/ErrorBoundary";
import {
  PrimaryNavigation,
  SecondaryNavigation,
  ViewScope,
} from "./components/ModuleNavigation";
import {
  buildNavigationSearch,
  defaultNavigationForModule,
  navigationForView,
  parseNavigation,
  parseNavigationSession,
  viewDefinition,
  type ModuleName,
  type NavigationState,
  type NavigationTarget,
  type ViewName,
} from "./navigation";
import { selectSessionUi, useChatStore } from "./stores/chatStore";
import { useSessionStore } from "./stores/sessionStore";

function StatusDot() {
  const activeId = useSessionStore((state) => state.activeId);
  const wsConnected = useChatStore((state) => state.wsConnected);
  const { isRunning, error } = useChatStore((state) => (
    selectSessionUi(state, activeId)
  ));
  if (!activeId) {
    return <span className="status-dot" style={{ background: "var(--text-muted)" }} />;
  }
  let className = "status-dot";
  if (error) className += " error";
  else if (isRunning) className += " busy";
  else if (!wsConnected) className += " error";
  return <span className={className} />;
}

function StatusText() {
  const activeId = useSessionStore((state) => state.activeId);
  const wsConnected = useChatStore((state) => state.wsConnected);
  const wsCloseInfo = useChatStore((state) => state.wsCloseInfo);
  const { isRunning, error } = useChatStore((state) => (
    selectSessionUi(state, activeId)
  ));
  if (!activeId) return <span id="status-text">No session selected</span>;
  if (error) {
    return <span id="status-text" style={{ color: "var(--error)" }}>{error}</span>;
  }
  if (isRunning) return <span id="status-text">Running…</span>;
  if (!wsConnected) {
    const detail = wsCloseInfo ? ` (${wsCloseInfo})` : "";
    return (
      <span id="status-text" style={{ color: "var(--error)" }}>
        WS disconnected{detail}
      </span>
    );
  }
  return <span id="status-text">Ready</span>;
}

function StatusCluster() {
  return (
    <div className="status-cluster">
      <StatusDot />
      <StatusText />
    </div>
  );
}

export default function App() {
  const [navigation, setNavigation] = useState<NavigationState>(
    () => parseNavigation(window.location.search),
  );
  const activeId = useSessionStore((state) => state.activeId);
  const openSession = useSessionStore((state) => state.openSession);
  const [leftCollapsed, setLeftCollapsed] = useState(false);
  const [rightCollapsed, setRightCollapsed] = useState(false);
  const lastViewByModule = useRef<Partial<Record<ModuleName, ViewName>>>({
    [navigation.module]: navigation.view,
  });
  const activeView = navigation.view;

  const writeNavigation = useCallback((
    next: NavigationState,
    mode: "push" | "replace" = "push",
    sessionId: string | null = useSessionStore.getState().activeId,
  ) => {
    lastViewByModule.current[next.module] = next.view;
    setNavigation(next);
    const search = buildNavigationSearch(
      window.location.search,
      next,
      sessionId,
    );
    window.history[mode === "push" ? "pushState" : "replaceState"](
      { module: next.module, view: next.view },
      "",
      `${window.location.pathname}${search}${window.location.hash}`,
    );
  }, []);

  const navigateView = useCallback((
    view: ViewName,
    target: NavigationTarget = {},
  ) => {
    writeNavigation(navigationForView(view, target));
  }, [writeNavigation]);

  const navigateModule = useCallback((moduleName: ModuleName) => {
    const remembered = lastViewByModule.current[moduleName];
    writeNavigation(
      remembered
        ? navigationForView(remembered)
        : defaultNavigationForModule(moduleName),
    );
  }, [writeNavigation]);

  const openSessionView = useCallback(async (
    sessionId: string,
    view: ViewName,
    target: NavigationTarget = {},
  ) => {
    await openSession(sessionId);
    if (useSessionStore.getState().activeId !== sessionId) return;
    writeNavigation(navigationForView(view, target), "push", sessionId);
  }, [openSession, writeNavigation]);

  useEffect(() => {
    const initialSession = parseNavigationSession(window.location.search);
    if (
      initialSession
      && initialSession !== useSessionStore.getState().activeId
    ) {
      void openSession(initialSession);
    }
    const handlePopState = () => {
      const next = parseNavigation(window.location.search);
      lastViewByModule.current[next.module] = next.view;
      setNavigation(next);
      const sessionId = parseNavigationSession(window.location.search);
      if (
        sessionId
        && sessionId !== useSessionStore.getState().activeId
      ) {
        void openSession(sessionId);
      }
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, [openSession]);

  useEffect(() => {
    const expected = buildNavigationSearch(
      window.location.search,
      navigation,
      activeId,
    );
    if (expected === window.location.search) return;
    window.history.replaceState(
      { module: navigation.module, view: navigation.view },
      "",
      `${window.location.pathname}${expected}${window.location.hash}`,
    );
  }, [activeId, navigation]);

  useEffect(() => {
    const view = viewDefinition(navigation.view);
    document.title = `${view.label} · Grace Code`;
  }, [navigation.view]);

  const appClass = [
    activeView === "chat" ? "has-event-sidebar" : "",
    leftCollapsed ? "left-collapsed" : "",
    rightCollapsed ? "right-collapsed" : "",
  ].filter(Boolean).join(" ");

  return (
    <div id="app-shell">
      <a className="skip-navigation-link" href="#main-workspace">
        Skip to main content
      </a>
      <div id="app" className={appClass}>
        <div className={`left-rail${leftCollapsed ? " collapsed" : ""}`}>
          {leftCollapsed ? (
            <div className="left-rail-collapsed-strip">
              <button
                className="sidebar-expand-btn"
                type="button"
                onClick={() => setLeftCollapsed(false)}
                aria-label="Expand sidebar"
              >
                →
              </button>
            </div>
          ) : (
            <ErrorBoundary>
              <SessionSidebar
                onToggleCollapse={() => setLeftCollapsed(true)}
                onOpenSession={(sessionId) => {
                  writeNavigation(
                    navigationForView("chat"),
                    "push",
                    sessionId,
                  );
                }}
              />
              <SessionTree />
            </ErrorBoundary>
          )}
        </div>

        <ErrorBoundary>
          <main
            id="main-workspace"
            className="main main-workbench"
            tabIndex={-1}
          >
            <header className="topbar topbar-workbench topbar-compact topbar-module-navigation">
              <div className="topbar-left">
                <PrimaryNavigation
                  activeModule={navigation.module}
                  onSelect={navigateModule}
                />
                <SecondaryNavigation
                  navigation={navigation}
                  onSelect={navigateView}
                />
              </div>
              <div className="topbar-right">
                <ViewScope navigation={navigation} />
                <StatusCluster />
                <ThemeToggle />
              </div>
            </header>

            <div className={`main-content main-content-${activeView}`}>
              {activeView === "overview" && (
                <ProjectOverview onNavigate={navigateView} />
              )}
              <div
                style={{
                  display: activeView === "chat" ? "flex" : "none",
                  flex: 1,
                  flexDirection: "column",
                  minHeight: 0,
                  overflow: "hidden",
                }}
              >
                <ChatView
                  key={activeId ?? "no-session"}
                  onInspectRun={(runId, turnId) => navigateView("runs", {
                    runId,
                    turnId,
                  })}
                />
              </div>
              {activeView === "reviews" && <DiffReviewView />}
              {activeView === "runs" && (
                <RunInspector
                  requestedRunId={navigation.runId}
                  onNavigate={navigateView}
                />
              )}
              {activeView === "context" && (
                <ContextInspector requestedRunId={navigation.runId} />
              )}
              {activeView === "evaluations" && (
                <EvaluationLab
                  onNavigate={navigateView}
                  onOpenHealth={() => navigateView("reliability")}
                />
              )}
              {activeView === "architecture" && <ArchitectureExplorer />}
              {activeView === "agents" && (
                <MultiAgentControlPlane
                  onOpenChanges={(sessionId) => {
                    void openSessionView(sessionId, "reviews");
                  }}
                />
              )}
              {activeView === "reliability" && (
                <ReliabilityDashboard
                  onOpenEvaluations={() => navigateView("evaluations")}
                />
              )}
              {activeView === "safety" && (
                <SafetyCenter
                  onInspectApproval={(sequence) => navigateView("events", {
                    sequence,
                  })}
                />
              )}
              {activeView === "replay" && (
                <ReplayLab
                  requestedRunId={navigation.runId}
                  onSelectRun={(runId, turnId) => navigateView("replay", {
                    runId,
                    turnId,
                  })}
                />
              )}
              {activeView === "plans" && <PlanLibrary />}
              {activeView === "memory" && <MemoryView />}
              {activeView === "events" && (
                <TraceView
                  requestedRunId={navigation.runId}
                  requestedSequence={navigation.sequence}
                  onShowSession={() => navigateView("events")}
                />
              )}
            </div>
          </main>
        </ErrorBoundary>

        {activeView === "chat" && !rightCollapsed && (
          <ErrorBoundary>
            <EventSidebar
              key={activeId ?? "no-session"}
              onToggleCollapse={() => setRightCollapsed(true)}
            />
          </ErrorBoundary>
        )}
        {activeView === "chat" && rightCollapsed && (
          <div className="right-rail-collapsed">
            <button
              className="sidebar-expand-btn"
              type="button"
              onClick={() => setRightCollapsed(false)}
              aria-label="Expand trace"
            >
              ←
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
