/**
 * TraceView — full-screen execution trace browser.
 *
 * Shows the complete event timeline for the active session with
 * step-based grouping, type filters, and live WS streaming.
 */
import { useEffect, useMemo, useState } from "react";
import { useSessionStore } from "../stores/sessionStore";
import { selectSessionUi, useChatStore } from "../stores/chatStore";
import { WsEventBlock } from "./WsEventBlock";
import { StateMachineInspector } from "./StateMachineInspector";
import type { WsMessage } from "../types";

type FilterValue = "all" | "thought" | "tool_call" | "observation" | "status" | "subagent";

const FILTERS: { key: FilterValue; label: string }[] = [
  { key: "all", label: "All" },
  { key: "thought", label: "Thoughts" },
  { key: "tool_call", label: "Actions" },
  { key: "observation", label: "Results" },
  { key: "status", label: "Status" },
  { key: "subagent", label: "Subagents" },
];

function formatDateTime(ts?: string) {
  if (!ts) return "—";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleTimeString();
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

interface TraceViewProps {
  requestedRunId?: string;
  requestedSequence?: number;
  onShowSession?: () => void;
}

export function TraceView({
  requestedRunId,
  requestedSequence,
  onShowSession,
}: TraceViewProps = {}) {
  const activeId = useSessionStore((s) => s.activeId);
  const activeDetail = useSessionStore((s) => s.activeDetail);
  const { events, isRunning, steps, tokens, timeline } = useChatStore((s) =>
    selectSessionUi(s, activeId),
  );
  const { loadTraceEvents } = useChatStore();
  const [filter, setFilter] = useState<FilterValue>("all");
  const [autoScroll, setAutoScroll] = useState(true);

  // Load historical events on mount
  useEffect(() => {
    if (activeId) {
      loadTraceEvents(activeId);
    }
  }, [activeId, loadTraceEvents]);

  // Filter events
  const filtered = useMemo(() => {
    const runEvents = requestedRunId
      ? events.filter((event) => event.run_id === requestedRunId)
      : events;
    if (filter === "all") return runEvents;
    if (filter === "subagent") return runEvents.filter((e) => e.type === "subagent_start" || e.type === "subagent_stop");
    return runEvents.filter((e) => e.type === filter);
  }, [events, filter, requestedRunId]);

  // Reverse for chronological display (oldest first for timeline)
  const chronological = useMemo(() => [...filtered].reverse(), [filtered]);

  // Group by step
  const stepGroups = useMemo(() => {
    const groups: { step: number; events: WsMessage[] }[] = [];
    let current: { step: number; events: WsMessage[] } | null = null;
    for (const ev of chronological) {
      const s = (ev as { step?: number }).step ?? 0;
      if (!current || current.step !== s) {
        current = { step: s, events: [] };
        groups.push(current);
      }
      current.events.push(ev);
    }
    return groups;
  }, [chronological]);

  // Stats
  const totalSteps = steps || activeDetail?.message_count || 0;
  const durationSeconds = deriveDurationSeconds(activeDetail?.created_at, activeDetail?.completed_at);
  const toolEvents = events.filter((e) => e.type === "tool_call").length;
  const observationEvents = events.filter((e) => e.type === "observation").length;

  // Auto-scroll to bottom when new events arrive
  useEffect(() => {
    if (autoScroll && isRunning) {
      const el = document.getElementById("trace-timeline-end");
      el?.scrollIntoView({ behavior: "smooth" });
    }
  }, [events.length, isRunning, autoScroll]);

  useEffect(() => {
    if (!requestedSequence) return;
    document.getElementById(`trace-event-${requestedSequence}`)
      ?.scrollIntoView({ block: "center" });
  }, [requestedSequence, chronological.length]);

  return (
    <section className="view active" data-view-name="events">
      <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}>
        {/* Header */}
        <div style={{
          padding: "14px 20px 10px",
          borderBottom: "1px solid var(--border)",
          background: "var(--bg-elev)",
          flexShrink: 0,
        }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
            <div>
              <div className="summary-label">Execution Trace</div>
              <h2 style={{ fontSize: 18, fontWeight: 700, color: "var(--text)", margin: "2px 0 0" }}>
                {activeDetail?.title || activeId?.slice(0, 8) || "No session selected"}
              </h2>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <span style={{
                display: "inline-flex", alignItems: "center", gap: 6,
                fontSize: 12, color: isRunning ? "var(--accent)" : "var(--text-muted)",
              }}>
                <span style={{
                  width: 8, height: 8, borderRadius: "50%",
                  background: isRunning ? "var(--accent)" : "var(--text-muted)",
                  animation: isRunning ? "pulse 1.5s ease-in-out infinite" : "none",
                }} />
                {isRunning ? "Live" : "Idle"}
              </span>
            </div>
          </div>

          {/* Quick stats */}
          <div style={{ display: "flex", gap: 16, fontSize: 12, color: "var(--text-muted)", flexWrap: "wrap" }}>
            <span>Steps: <strong style={{ color: "var(--text)" }}>{totalSteps}</strong></span>
            <span>Events: <strong style={{ color: "var(--text)" }}>{events.length}</strong></span>
            <span>Tools: <strong style={{ color: "var(--text)" }}>{toolEvents}</strong></span>
            <span>Observations: <strong style={{ color: "var(--text)" }}>{observationEvents}</strong></span>
            <span>Duration: <strong style={{ color: "var(--text)" }}>{formatDuration(durationSeconds)}</strong></span>
            {tokens > 0 && <span>Tokens: <strong style={{ color: "var(--text)" }}>{tokens.toLocaleString()}</strong></span>}
            {activeDetail?.total_tokens_estimate != null && activeDetail.total_tokens_estimate > 0 && (
              <span>Context: <strong style={{ color: "var(--text)" }}>{(activeDetail.total_tokens_estimate / 1000).toFixed(1)}K</strong></span>
            )}
          </div>
        </div>

        {/* Filter bar */}
        {requestedRunId && (
          <div className="evidence-target-banner">
            <span>Filtered to run <code>{requestedRunId}</code></span>
            {onShowSession && (
              <button type="button" onClick={onShowSession}>Show full session</button>
            )}
          </div>
        )}
        <div className="trace-filter-bar" style={{
          padding: "8px 20px",
          borderBottom: "1px solid var(--border)",
          background: "var(--bg)",
          display: "flex", alignItems: "center", gap: 8,
          flexShrink: 0,
        }}>
          {FILTERS.map((f) => (
            <button
              key={f.key}
              type="button"
              className={`event-filter ${filter === f.key ? "active" : ""}`}
              onClick={() => setFilter(f.key)}
              style={{ fontSize: 11, padding: "3px 10px" }}
            >
              {f.label}
            </button>
          ))}
          <div style={{ flex: 1 }} />
          <label style={{ fontSize: 11, color: "var(--text-muted)", display: "flex", alignItems: "center", gap: 4, cursor: "pointer" }}>
            <input type="checkbox" checked={autoScroll} onChange={(e) => setAutoScroll(e.target.checked)} />
            Auto-scroll
          </label>
        </div>

        {/* Timeline */}
        <div
          key={filter}
          className="trace-timeline"
          data-filter={filter}
          style={{ flex: 1, overflow: "auto", padding: "16px 20px" }}
        >
          {!activeId && (
            <div className="plan-empty">
              <div className="plan-empty-icon">E</div>
              <div className="plan-empty-title">No session selected</div>
              <div className="plan-empty-body">Select a session from the sidebar to view its execution trace.</div>
            </div>
          )}

          {activeId && chronological.length === 0 && !isRunning && (
            <div className="plan-empty">
              <div className="plan-empty-icon">E</div>
              <div className="plan-empty-title">No events recorded</div>
              <div className="plan-empty-body">
                Events will appear here once the agent starts executing. Start a chat to begin.
              </div>
            </div>
          )}

          {activeId && chronological.length === 0 && isRunning && (
            <div style={{ textAlign: "center", padding: 40, color: "var(--text-muted)" }}>
              Waiting for execution events...
            </div>
          )}

          {stepGroups.map((group) => (
            <div key={group.step} style={{ marginBottom: 4 }}>
              {/* Step marker */}
              {group.step > 0 && (
                <div style={{
                  display: "flex", alignItems: "center", gap: 10,
                  padding: "6px 0", marginBottom: 4,
                  position: "sticky", top: 0, zIndex: 1,
                  background: "var(--bg)",
                }}>
                  <span style={{
                    fontSize: 10, fontWeight: 700, color: "var(--accent)",
                    textTransform: "uppercase", letterSpacing: "0.5px",
                    background: "var(--accent-soft)", padding: "2px 8px", borderRadius: 3,
                  }}>
                    Step {group.step}
                  </span>
                  <span style={{ flex: 1, height: 1, background: "var(--border)" }} />
                  <span style={{ fontSize: 10, color: "var(--text-dim)" }}>
                    {group.events.length} event{group.events.length !== 1 ? "s" : ""}
                  </span>
                </div>
              )}
              {group.events.map((ev, i) => (
                <div
                  id={ev.sequence ? `trace-event-${ev.sequence}` : undefined}
                  className={ev.sequence === requestedSequence ? "trace-event-target" : ""}
                  key={`${group.step}-${i}`}
                >
                  <WsEventBlock event={ev} />
                </div>
              ))}
            </div>
          ))}

          {/* TSM State Inspector (collapsible) */}
          <StateMachineInspector />
          <div id="trace-timeline-end" />
        </div>
      </div>
    </section>
  );
}
