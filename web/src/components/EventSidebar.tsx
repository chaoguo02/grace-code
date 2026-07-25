import { useEffect, useState, useCallback } from "react";
import { selectSessionUi, useChatStore } from "../stores/chatStore";
import { useSessionStore } from "../stores/sessionStore";
import type { WsMessage } from "../types";
import { getStorageStats } from "../api/storage";
import { getSessionStats } from "../api/stats";

interface StorageStats {
  backend: string;
  total_sessions: number;
  total_messages: number;
  total_memories?: number;
  db_size_bytes: number | null;
}

interface SessionStats {
  steps_taken?: number;
  max_steps?: number;
  total_tokens?: number;
  duration_seconds?: number;
  tools?: Record<string, number>;
}

function formatTimeLabel(ev: { timestamp?: string }, index: number) {
  if (ev.timestamp) {
    const d = new Date(ev.timestamp);
    if (!Number.isNaN(d.getTime())) {
      return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    }
  }
  // Fallback: synthetic relative label
  const now = new Date();
  now.setSeconds(now.getSeconds() - index * 28);
  return now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function formatDuration(seconds?: number | null) {
  if (seconds == null || Number.isNaN(seconds)) return "00:00";
  const total = Math.max(0, Math.floor(seconds));
  const min = Math.floor(total / 60);
  const sec = total % 60;
  return `${String(min).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

function deriveDurationSeconds(createdAt?: string | null, completedAt?: string | null) {
  if (!createdAt) return 0;
  const start = new Date(createdAt).getTime();
  if (Number.isNaN(start)) return 0;
  const end = completedAt ? new Date(completedAt).getTime() : Date.now();
  if (Number.isNaN(end)) return 0;
  return Math.max(0, Math.floor((end - start) / 1000));
}

function countTools(events: WsMessage[]) {
  const counts: Record<string, number> = {};
  for (const ev of events) {
    if (ev.type !== "tool_call") continue;
    const name = ev.name || "Tool";
    counts[name] = (counts[name] || 0) + 1;
  }
  return counts;
}

export function EventSidebar({ onToggleCollapse }: { onToggleCollapse?: () => void }) {
  const activeId = useSessionStore((s) => s.activeId);
  const { events, isRunning, steps, tokens } = useChatStore((s) => selectSessionUi(s, activeId));
  const sessionCount = useSessionStore((s) => s.sessions.length);
  const activeDetail = useSessionStore((s) => s.activeDetail);
  const [stats, setStats] = useState<StorageStats | null>(null);
  const [sessionStats, setSessionStats] = useState<SessionStats | null>(null);

  // Fetch storage stats once on mount — not per-session.
  useEffect(() => {
    const controller = new AbortController();
    getStorageStats(controller.signal)
      .then(setStats)
      .catch(() => {});
    return () => controller.abort();
  }, []);  // empty deps: fire once, never refire

  // Fetch persisted session stats when activeId changes (baseline).
  // Live execution stats follow the WS stream — no polling.
  useEffect(() => {
    if (!activeId) {
      setSessionStats(null);
      return;
    }
    let cancelled = false;
    getSessionStats(activeId)
      .then((data) => {
        if (!cancelled && data) setSessionStats(data);
      })
      .catch(() => {
        if (!cancelled) setSessionStats(null);
      });
    return () => {
      cancelled = true;
    };
  }, [activeId]);

  const toolCounts = sessionStats?.tools && Object.keys(sessionStats.tools).length
    ? sessionStats.tools
    : countTools(events);
  const sortedTools = Object.entries(toolCounts).sort((a, b) => b[1] - a[1]).slice(0, 4);
  const totalToolCalls = sortedTools.reduce((sum, [, count]) => sum + count, 0);
  const totalSteps = sessionStats?.steps_taken ?? steps ?? activeDetail?.message_count ?? 0;
  const maxSteps = sessionStats?.max_steps ?? 10;
  const totalTokens = sessionStats?.total_tokens ?? tokens ?? activeDetail?.total_tokens_estimate ?? 0;
  const durationSeconds = sessionStats?.duration_seconds ?? deriveDurationSeconds(activeDetail?.created_at, activeDetail?.completed_at);
  const progressRatio = Math.min(100, Math.max(0, maxSteps ? Math.round((totalSteps / maxSteps) * 100) : 0));

  return (
    <aside className="event-sidebar" id="event-sidebar">
      <div className="event-header">
        <div className="event-header-topline">
          <div className="event-title">Live Trace</div>
          <button className="event-header-action" type="button" aria-label="Collapse live trace" onClick={onToggleCollapse}>
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
            {isRunning ? "Agent is running..." : "Trace is idle"}
          </div>
          <div className="trace-hero-copy">
            {isRunning ? "Waiting for next event" : "Start a run to populate the live timeline."}
          </div>
        </div>
      </div>

      <div className="event-filter-row" style={{ justifyContent: "flex-end" }}>
        <a href="#" onClick={(e) => { e.preventDefault(); /* TODO: switch to Trace tab */ }} style={{ fontSize: 10, color: "var(--text-muted)" }}>
          Open Full Trace →
        </a>
      </div>

      <div className="execution-stats-card">
        <div className="execution-stats-title">Execution Stats</div>
        <div className="execution-stats-list">
          <div className="execution-stats-row execution-stats-steps">
            <span>Steps</span>
            <div className="execution-stats-value-group">
              <strong>{totalSteps} / {maxSteps}</strong>
              <div className="execution-mini-progress">
                <div className="execution-mini-progress-fill" style={{ width: `${progressRatio}%` }} />
              </div>
            </div>
          </div>
          <div className="execution-stats-row">
            <span>Duration</span>
            <strong>{formatDuration(durationSeconds)}</strong>
          </div>
          <div className="execution-stats-row">
            <span>Tokens</span>
            <strong>{totalTokens.toLocaleString()}</strong>
          </div>
          <div className="execution-stats-row execution-stats-tools">
            <span>Tools</span>
            <div className="execution-tool-list">
              {sortedTools.length === 0 ? (
                <strong>—</strong>
              ) : (
                sortedTools.map(([name, count]) => (
                  <div key={name} className="execution-tool-row">
                    <span>{name}</span>
                    <div className="execution-tool-bar-wrap">
                      <div
                        className="execution-tool-bar"
                        style={{ width: `${totalToolCalls ? Math.max(16, Math.round((count / totalToolCalls) * 100)) : 16}%` }}
                      />
                    </div>
                    <strong>{count}</strong>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      </div>

      <div className="event-list event-timeline">
        {events.length === 0 && (
          <div className="empty-state">Waiting for execution...</div>
        )}

        {events.slice(0, 5).map((ev, i) => {
          const icon = ev.type === "tool_call" ? "⚙" :
                       ev.type === "thought" ? "▸" :
                       ev.type === "observation" ? "○" : "•";
          const label = ev.type === "tool_call" ? (ev.name || "Tool") :
                        ev.type === "thought" ? "Thought" :
                        ev.type === "observation" ? (ev.tool_name || "Result") : (ev.type || "Event");
          return (
            <div key={i} className="timeline-row-compact">
              <span className="timeline-compact-icon">{icon}</span>
              <span className="timeline-compact-label">{label}</span>
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
            {stats.total_memories != null && (
              <div className="resource-row">
                <span>Memories</span>
                <strong>{stats.total_memories}</strong>
              </div>
            )}
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
