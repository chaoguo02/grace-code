import { useState } from "react";
import { SessionSidebar } from "./components/SessionSidebar";
import { ChatView } from "./components/ChatView";
import { EventSidebar } from "./components/EventSidebar";
import { ThemeToggle } from "./components/ThemeToggle";

const TABS = [
  { key: "chat", label: "Chat" },
  { key: "tasks", label: "Tasks" },
  { key: "plan", label: "Plan" },
  { key: "events", label: "Events" },
] as const;

type ViewName = (typeof TABS)[number]["key"];

function PlaceholderView({ name }: { name: string }) {
  return (
    <section className="view active" data-view-name={name}>
      <div style={{ padding: 20, color: "var(--text-dim)" }}>
        {name.charAt(0).toUpperCase() + name.slice(1)} view — coming soon.
      </div>
    </section>
  );
}

export default function App() {
  const [activeView, setActiveView] = useState<ViewName>("chat");

  return (
    <div id="app" className={activeView === "chat" ? "has-event-sidebar" : ""}>
      <SessionSidebar />

      <main className="main">
        <header className="topbar">
          <div className="topbar-left">
            <span className="status-dot" />
            <span id="status-text">Ready</span>
            <div className="view-tabs">
              {TABS.map((tab) => (
                <button
                  key={tab.key}
                  className={`view-tab ${activeView === tab.key ? "active" : ""}`}
                  data-view={tab.key}
                  type="button"
                  onClick={() => setActiveView(tab.key)}
                >
                  {tab.label}
                </button>
              ))}
            </div>
          </div>
          <div className="topbar-right">
            <ThemeToggle />
          </div>
        </header>

        {activeView === "chat" && <ChatView />}
        {activeView !== "chat" && <PlaceholderView name={activeView} />}
      </main>

      {activeView === "chat" && <EventSidebar />}
    </div>
  );
}
