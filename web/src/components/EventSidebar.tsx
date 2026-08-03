import { useEffect, useState, useMemo } from "react";
import { selectSessionUi, useChatStore } from "../stores/chatStore";
import { useSessionStore } from "../stores/sessionStore";
import { WsEventBlock } from "./WsEventBlock";
import type { WsMessage } from "../types";
import { getSessionStats } from "../api/stats";

interface SessionStats {
  steps_taken?: number;
  max_steps?: number;
  total_tokens?: number;
  duration_seconds?: number;
  tools?: Record<string, number>;
}

type FilterValue = "all" | "thought" | "tool_call" | "observation" | "status" | "subagent";

const FILTERS: { key: FilterValue; label: string }[] = [
  { key: "all", label: "All" },
  { key: "thought", label: "Thoughts" },
  { key: "tool_call", label: "Actions" },
  { key: "observation", label: "Results" },
  { key: "status", label: "Status" },
  { key: "subagent", label: "Subagents" },
];

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
  const activeDetail = useSessionStore((s) => s.activeDetail);
  const { loadTraceEvents } = useChatStore();
  const [sessionStats, setSessionStats] = useState<SessionStats | null>(null);
  const [traceExpanded, setTraceExpanded] = useState(false);
  const [filter, setFilter] = useState<FilterValue>("all");

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

  // Load the persisted event backlog only when the full timeline is expanded.
  useEffect(() => {
    if (activeId && traceExpanded) {
      loadTraceEvents(activeId);
    }
  }, [activeId, traceExpanded, loadTraceEvents]);

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

  // Filtered, chronological event list for the expanded timeline.
  const filteredTimeline = useMemo(() => {
    const base = [...events].reverse();
    if (filter === "all") return base;
    if (filter === "subagent") return base.filter((e) => e.type === "subagent_start" || e.type === "subagent_stop");
    return base.filter((e) => e.type === filter);
  }, [events, filter]);

  const previewEvents = events.slice(0, 5);

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
        <button
          type="button"
          className="event-expand-toggle"
          onClick={() => setTraceExpanded((v) => !v)}
          aria-expanded={traceExpanded}
        >
          {traceExpanded ? "Collapse full trace −" : "Expand full trace +"}
        </button>
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

      {traceExpanded ? (
        <div className="event-timeline-full">
          <div className="event-trace-filter-row">
            {FILTERS.map((f) => (
              <button
                key={f.key}
                type="button"
                className={`event-filter ${filter === f.key ? "active" : ""}`}
                onClick={() => setFilter(f.key)}
              >
                {f.label}
              </button>
            ))}
          </div>
          <div className="event-list event-timeline" style={{ maxHeight: "56dvh", overflow: "auto" }}>
            {events.length === 0 && (
              <div className="empty-state">Waiting for execution...</div>
            )}
            {filteredTimeline.map((ev, i) => (
              <div key={`${ev.sequence ?? i}-${i}`}>
                <WsEventBlock event={ev} />
              </div>
            ))}
          </div>
        </div>
      ) : (
        <div className="event-list event-timeline">
          {events.length === 0 && (
            <div className="empty-state">Waiting for execution...</div>
          )}
          {previewEvents.map((ev, i) => {
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
      )}
    </aside>
  );
}
