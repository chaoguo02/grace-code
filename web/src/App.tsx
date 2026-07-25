import { useState } from "react";
import { SessionSidebar } from "./components/SessionSidebar";
import { SessionTree } from "./components/SessionTree";
import { ChatView } from "./components/ChatView";
import { DiffReviewView } from "./components/DiffReviewView";
import { PlanLibrary } from "./components/PlanLibrary";
import { MemoryView } from "./components/MemoryView";
import { TraceView } from "./components/TraceView";
import { EventSidebar } from "./components/EventSidebar";
import { ThemeToggle } from "./components/ThemeToggle";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { selectSessionUi, useChatStore } from "./stores/chatStore";
import { useSessionStore } from "./stores/sessionStore";

const TABS = [
  { key: "chat", label: "Chat" },
  { key: "reviews", label: "Review" },
  { key: "plans", label: "Plans" },
  { key: "memory", label: "Memory" },
  { key: "events", label: "Trace" },
] as const;

type ViewName = (typeof TABS)[number]["key"];

function TabIcon({ name }: { name: ViewName }) {
  if (name === "chat") return <span className="tab-icon">C</span>;
  if (name === "reviews") return <span className="tab-icon">R</span>;
  if (name === "plans") return <span className="tab-icon">P</span>;
  if (name === "memory") return <span className="tab-icon">◎</span>;
  if (name === "events") return <span className="tab-icon">E</span>;
  return <span className="tab-icon">?</span>;
}

function StatusDot() {
  const activeId = useSessionStore((s) => s.activeId);
  const wsConnected = useChatStore((s) => s.wsConnected);
  const { isRunning, error } = useChatStore((s) => selectSessionUi(s, activeId));
  if (!activeId) {
    return <span className="status-dot" style={{ background: "var(--text-muted)" }} />;
  }
  let cls = "status-dot";
  if (error) cls += " error";
  else if (isRunning) cls += " busy";
  else if (!wsConnected) cls += " error";
  return <span className={cls} />;
}

function StatusText() {
  const activeId = useSessionStore((s) => s.activeId);
  const wsConnected = useChatStore((s) => s.wsConnected);
  const wsCloseInfo = useChatStore((s) => s.wsCloseInfo);
  const { isRunning, error } = useChatStore((s) => selectSessionUi(s, activeId));
  if (!activeId) return <span id="status-text">No session selected</span>;
  if (error) return <span id="status-text" style={{ color: "var(--error)" }}>{error}</span>;
  if (isRunning) return <span id="status-text">Running…</span>;
  if (!wsConnected) {
    const detail = wsCloseInfo ? ` (${wsCloseInfo})` : "";
    return <span id="status-text" style={{ color: "var(--error)" }}>WS disconnected{detail}</span>;
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
  const [activeView, setActiveView] = useState<ViewName>("chat");
  const activeId = useSessionStore((s) => s.activeId);
  const [leftCollapsed, setLeftCollapsed] = useState(false);
  const [rightCollapsed, setRightCollapsed] = useState(false);

  const appClass = [
    activeView === "chat" ? "has-event-sidebar" : "",
    leftCollapsed ? "left-collapsed" : "",
    rightCollapsed ? "right-collapsed" : "",
  ].filter(Boolean).join(" ");

  return (
    <div id="app-shell">
      <div id="app" className={appClass}>
        <div className={`left-rail${leftCollapsed ? " collapsed" : ""}`}>
          {leftCollapsed ? (
            <div className="left-rail-collapsed-strip">
              <button className="sidebar-expand-btn" type="button" onClick={() => setLeftCollapsed(false)} aria-label="Expand sidebar">›</button>
            </div>
          ) : (
            <ErrorBoundary>
              <SessionSidebar onToggleCollapse={() => setLeftCollapsed(true)} />
              <SessionTree />
            </ErrorBoundary>
          )}
        </div>

        <ErrorBoundary>
          <main className="main main-workbench">
            <header className="topbar topbar-workbench topbar-compact">
              <div className="topbar-left">
                <div className="view-tabs">
                  {TABS.map((tab) => (
                    <button
                      key={tab.key}
                      className={`view-tab ${activeView === tab.key ? "active" : ""}`}
                      data-view={tab.key}
                      type="button"
                      onClick={() => setActiveView(tab.key)}
                    >
                      <TabIcon name={tab.key} />
                      {tab.label}
                    </button>
                  ))}
                </div>
              </div>
              <div className="topbar-right">
                <StatusCluster />
                <ThemeToggle />
              </div>
            </header>

            <div className={`main-content main-content-${activeView}`}>
              <div style={{ display: activeView === "chat" ? "flex" : "none", flex: 1, flexDirection: "column", minHeight: 0, overflow: "hidden" }}>
                <ChatView key={activeId ?? "no-session"} />
              </div>
              {activeView === "reviews" && <DiffReviewView />}
              {activeView === "plans" && <PlanLibrary />}
              {activeView === "memory" && <MemoryView />}
              {activeView === "events" && <TraceView />}
            </div>
          </main>
        </ErrorBoundary>

        {activeView === "chat" && !rightCollapsed && <ErrorBoundary><EventSidebar key={activeId ?? "no-session"} onToggleCollapse={() => setRightCollapsed(!rightCollapsed)} /></ErrorBoundary>}
        {activeView === "chat" && rightCollapsed && (
          <div className="right-rail-collapsed">
            <button className="sidebar-expand-btn" type="button" onClick={() => setRightCollapsed(false)} aria-label="Expand trace">‹</button>
          </div>
        )}
      </div>
    </div>
  );
}



