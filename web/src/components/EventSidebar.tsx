import { useEffect, useState, useCallback } from "react";
import { useChatStore } from "../stores/chatStore";
import { useSessionStore } from "../stores/sessionStore";

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

function formatTimeLabel(index: number) {
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

function countTools(events: ReturnType<typeof useChatStore.getState>["events"]) {
  const counts: Record<string, number> = {};
  for (const ev of events) {
    if (ev.type !== "tool_call") continue;
    const name = ev.name || "Tool";
    counts[name] = (counts[name] || 0) + 1;
  }
  return counts;
}

export function EventSidebar() {
  const events = useChatStore((s) => s.events);
  const isRunning = useChatStore((s) => s.isRunning);
  const steps = useChatStore((s) => s.steps);
  const tokens = useChatStore((s) => s.tokens);
  const activeId = useSessionStore((s) => s.activeId);
  const sessionCount = useSessionStore((s) => s.sessions.length);
  const activeDetail = useSessionStore((s) => s.activeDetail);
  const [stats, setStats] = useState<StorageStats | null>(null);
  const [sessionStats, setSessionStats] = useState<SessionStats | null>(null);
  const [eventFilter, setEventFilter] = useState<string>("all");

  const fetchStats = useCallback(() => {
    fetch("/api/storage/stats")
      .then((r) => r.json())
      .then(setStats)
      .catch(() => {});
  }, []);

  useEffect(() => {
    fetchStats();
  }, [fetchStats, activeId, sessionCount]);

  // Debounced stats fetch — avoids request storm on rapid events
  useEffect(() => {
    if (!activeId) {
      setSessionStats(null);
      return;
    }
    const timer = setTimeout(() => {
      let cancelled = false;
      fetch(`/api/sessions/${encodeURIComponent(activeId)}/stats`)
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          if (!cancelled && data) setSessionStats(data);
        })
        .catch(() => {
          if (!cancelled) setSessionStats(null);
        });
      return () => { cancelled = true; };
    }, 3000);  // debounce 3s
    return () => clearTimeout(timer);
  }, [activeId, steps, tokens, events.length]);

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
    (ev as { content?: string }).content?.slice(0, 72) ||
    (ev as { name?: string }).name?.slice(0, 72) ||
    (ev as { output?: string }).output?.slice(0, 72) ||
    (ev as { error?: string }).error?.slice(0, 72) ||
    (ev as { message?: string }).message?.slice(0, 72) ||
    "Waiting for details";

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
          <button className="event-header-action" type="button" aria-label="Collapse live trace">
            ‹
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

      <div className="event-filter-row">
        {(["all", "steps", "logs", "files"] as const).map((f) => (
          <button key={f} className={`event-filter ${eventFilter === f ? "active" : ""}`}
            type="button" onClick={() => setEventFilter(f)}>
            {f === "all" ? "All" : f === "steps" ? "Steps" : f === "logs" ? "Logs" : "Files"}
          </button>
        ))}
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

        {events
          .filter((ev) => {
            if (eventFilter === "all") return true;
            if (eventFilter === "steps") return ev.type === "tool_call" || ev.type === "observation";
            if (eventFilter === "logs") return ev.type === "thought" || ev.type === "reflection" || ev.type === "status";
            if (eventFilter === "files") return ev.type === "observation" && !!ev.diff;
            return true;
          })
          .map((ev, i) => {
          return (
            <div key={i} className="timeline-row">
              <div className="timeline-time">{formatTimeLabel(i)}</div>
              <div className="timeline-node" />
              <div className="timeline-card">
                <div className="timeline-card-head">
                  <div className="timeline-card-icon">
                    {ev.type === "thought" ? "T" : ev.type === "tool_call" ? "A" : "O"}
                  </div>
                  <div className="timeline-card-title">{renderTitle(ev)}</div>
                  <div className="timeline-card-status">•</div>
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
