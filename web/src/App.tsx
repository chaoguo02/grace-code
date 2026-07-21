import { useState } from "react";
import { SessionSidebar } from "./components/SessionSidebar";
import { SessionTree } from "./components/SessionTree";
import { ChatView } from "./components/ChatView";
import { PlanView } from "./components/PlanView";
import { DiffReviewView } from "./components/DiffReviewView";
import { StatsDashboard } from "./components/StatsDashboard";
import { MemoryView } from "./components/MemoryView";
import { EventSidebar } from "./components/EventSidebar";
import { ThemeToggle } from "./components/ThemeToggle";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { selectSessionUi, useChatStore } from "./stores/chatStore";
import { useSessionStore } from "./stores/sessionStore";

const TABS = [
  { key: "chat", label: "Chat" },
  { key: "plan", label: "Plan" },
  { key: "reviews", label: "Reviews" },
  { key: "stats", label: "Stats" },
  { key: "memory", label: "Memory" },
] as const;

type ViewName = (typeof TABS)[number]["key"];

function TabIcon({ name }: { name: ViewName }) {
  if (name === "chat") return <span className="tab-icon">C</span>;
  if (name === "plan") return <span className="tab-icon">P</span>;
  if (name === "reviews") return <span className="tab-icon">R</span>;
  if (name === "stats") return <span className="tab-icon">S</span>;
  if (name === "memory") return <span className="tab-icon">◎</span>;
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

  return (
    <div id="app-shell">
      <div id="app" className={activeView === "chat" ? "has-event-sidebar" : ""}>
        <div className="left-rail">
          <ErrorBoundary>
            <SessionSidebar />
            <SessionTree />
          </ErrorBoundary>
        </div>

        <ErrorBoundary>
          <main className="main">
            <header className="topbar">
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
                <button className="topbar-action" type="button">
                  Share
                </button>
                <button className="topbar-icon-action" type="button" aria-label="More actions">
                  ...
                </button>
                <ThemeToggle />
              </div>
            </header>

            <div style={{ display: activeView === "chat" ? "flex" : "none", flex: 1, flexDirection: "column", minHeight: 0, overflow: "hidden" }}>
              <ChatView key={activeId ?? "no-session"} />
            </div>
            {activeView === "plan" && <PlanView />}
            {activeView === "reviews" && <DiffReviewView />}
            {activeView === "stats" && <StatsDashboard />}
            {activeView === "memory" && <MemoryView />}
          </main>
        </ErrorBoundary>

        {activeView === "chat" && <ErrorBoundary><EventSidebar key={activeId ?? "no-session"} /></ErrorBoundary>}
      </div>
    </div>
  );
}



