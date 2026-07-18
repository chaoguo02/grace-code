import { useEffect, useState, useCallback } from "react";
import { useChatStore } from "../stores/chatStore";
import { useSessionStore } from "../stores/sessionStore";

interface StorageStats {
  backend: string;
  total_sessions: number;
  total_messages: number;
  db_size_bytes: number | null;
}

function formatTimeLabel(index: number) {
  const now = new Date();
  now.setSeconds(now.getSeconds() - index * 28);
  return now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function EventSidebar() {
  const events = useChatStore((s) => s.events);
  const isRunning = useChatStore((s) => s.isRunning);
  const activeId = useSessionStore((s) => s.activeId);
  const sessionCount = useSessionStore((s) => s.sessions.length);
  const [stats, setStats] = useState<StorageStats | null>(null);

  const fetchStats = useCallback(() => {
    fetch("/api/storage/stats")
      .then((r) => r.json())
      .then(setStats)
      .catch(() => {});
  }, []);

  useEffect(() => {
    fetchStats();
  }, [fetchStats, activeId, sessionCount]);

  const renderTitle = (ev: (typeof events)[number]) => {
    if (ev.type === "thought") return "Planning";
    if (ev.type === "tool_call") return ev.name || "Tool Call";
    if (ev.type === "observation") return ev.tool_name || "Observation";
    if (ev.type === "reflection") return "Reflection";
    if (ev.type === "subagent_start") return "Subagent Started";
    if (ev.type === "subagent_stop") return "Subagent Finished";
    return ev.type || "Event";
  };

  const renderPreview = (ev: (typeof events)[number]) =>
    ev.content?.slice(0, 72) ||
    ev.name?.slice(0, 72) ||
    ev.output?.slice(0, 72) ||
    ev.error?.slice(0, 72) ||
    ev.message?.slice(0, 72) ||
    "Waiting for details";

  return (
    <aside className="event-sidebar" id="event-sidebar">
      <div className="event-header">
        <div className="event-header-topline">
          <div className="event-title">Live Trace</div>
          <button className="event-header-action" type="button" aria-label="Collapse live trace">
            ›
          </button>
        </div>
        <div className="event-subtitle">
          Real-time execution events from the agent workspace.
        </div>
      </div>

      <div className="trace-hero-card">
        <div className={`trace-hero-spinner ${isRunning ? "running" : ""}`} />
        <div>
          <div className="trace-hero-title">
            {isRunning ? "Agent is running…" : "Trace is idle"}
          </div>
          <div className="trace-hero-copy">
            {isRunning ? "Waiting for next event" : "Start a run to populate the live timeline."}
          </div>
        </div>
      </div>

      <div className="event-filter-row">
        <button className="event-filter active" type="button">All</button>
        <button className="event-filter" type="button">Steps</button>
        <button className="event-filter" type="button">Logs</button>
        <button className="event-filter" type="button">Files</button>
      </div>

      <div className="event-list event-timeline">
        {events.length === 0 && (
          <div className="empty-state">Waiting for execution…</div>
        )}

        {events.map((ev, i) => {
          return (
            <div key={i} className="timeline-row">
              <div className="timeline-time">{formatTimeLabel(i)}</div>
              <div className="timeline-node" />
              <div className="timeline-card">
                <div className="timeline-card-head">
                  <div className="timeline-card-icon">
                    {ev.type === "thought" ? "✦" : ev.type === "tool_call" ? "⌕" : "↳"}
                  </div>
                  <div className="timeline-card-title">{renderTitle(ev)}</div>
                  <div className="timeline-card-status">○</div>
                </div>
                <div className="timeline-card-body">{renderPreview(ev)}</div>
              </div>
            </div>
          );
        })}
      </div>

      {stats && (
        <div className="storage-card resource-card">
          <div className="resource-card-title">Session Resources</div>
          <div className="resource-list">
            <div className="resource-row">
              <span>Sessions tracked</span>
              <strong>{stats.total_sessions}</strong>
            </div>
            <div className="resource-row">
              <span>Messages stored</span>
              <strong>{stats.total_messages}</strong>
            </div>
            <div className="resource-row">
              <span>Storage backend</span>
              <strong>{stats.backend}</strong>
            </div>
            {stats.db_size_bytes != null && (
              <div className="resource-row">
                <span>DB size</span>
                <strong>{(stats.db_size_bytes / 1024).toFixed(0)} KB</strong>
              </div>
            )}
          </div>
        </div>
      )}
    </aside>
  );
}
