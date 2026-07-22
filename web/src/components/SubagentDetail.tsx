/**
 * SubagentDetail — child session execution log viewer.
 *
 * CC-aligned: shows full timeline of a subagent session with
 * back-to-parent navigation.  Opens when user clicks a child
 * session in SessionTree or a subagent_stop event in the timeline.
 *
 * Batch 3: added summary stats card and event-type filtering.
 *
 * CSS classes: .subagent-detail-*, .subagent-summary-*  (Phase 7 Batch B + Batch 3)
 */
import { useEffect, useMemo, useState } from "react";
import { useSessionStore } from "../stores/sessionStore";
import { selectSessionUi, useChatStore } from "../stores/chatStore";
import { WsEventBlock } from "./WsEventBlock";
import * as api from "../api/sessions";
import type { WsMessage, SessionDetail } from "../types";

interface SubagentDetailProps {
  childSessionId: string;
  onClose: () => void;
}

type EventFilter = "all" | "thought" | "tool_call" | "observation" | "plan_ready" | "status";

const EVENT_FILTER_OPTIONS: Array<{ key: EventFilter; label: string }> = [
  { key: "all", label: "All" },
  { key: "thought", label: "Thoughts" },
  { key: "tool_call", label: "Actions" },
  { key: "observation", label: "Results" },
  { key: "plan_ready", label: "Plans" },
  { key: "status", label: "Status" },
];

function formatDuration(seconds?: number | null): string {
  if (seconds == null || seconds <= 0) return "—";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}m ${s}s`;
}

function countByType(events: WsMessage[], type: string): number {
  return events.filter((e) => e.type === type).length;
}

export function SubagentDetail({ childSessionId, onClose }: SubagentDetailProps) {
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [events, setEvents] = useState<WsMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [retryKey, setRetryKey] = useState(0);
  const [worktreeAction, setWorktreeAction] = useState<string | null>(null);
  const [eventFilter, setEventFilter] = useState<EventFilter>("all");
  const activeId = useSessionStore((s) => s.activeId);
  const worktreeStates = useChatStore((s) => selectSessionUi(s, activeId).worktreeStates);

  async function handleWorktree(action: string) {
    if (!activeId) return;
    setWorktreeAction(action);
    try {
      await api.resolveWorktree(activeId, childSessionId, action);
    } catch {
      setWorktreeAction(null);
    }
  }

  const hasWorktree = detail?.worktree_disposition === "preserved";
  const resolvedStatus =
    worktreeStates[`${childSessionId}_${worktreeAction}`] ||
    worktreeStates[`${childSessionId}_apply`] ||
    worktreeStates[`${childSessionId}_discard`] ||
    worktreeStates[`${childSessionId}_retain`];
  const isResolved =
    resolvedStatus === "applied" || resolvedStatus === "discarded" || resolvedStatus === "retained";
  const isFailed = resolvedStatus === "error";

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      try {
        const [d, evs] = await Promise.all([
          api.getSession(childSessionId),
          api.getTraceEvents(childSessionId),
        ]);
        if (!cancelled) {
          setDetail(d);
          setEvents(evs);
        }
      } catch {
        if (!cancelled) setLoadError(true);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, [childSessionId, retryKey]);

  const filteredEvents = useMemo(() => {
    if (eventFilter === "all") return events;
    return events.filter((e) => e.type === eventFilter);
  }, [events, eventFilter]);

  // Derived stats
  const toolCalls = useMemo(() => countByType(events, "tool_call"), [events]);
  const observations = useMemo(() => countByType(events, "observation"), [events]);
  const thoughts = useMemo(() => countByType(events, "thought"), [events]);
  const durationLabel = formatDuration(
    detail?.created_at && detail?.completed_at
      ? Math.round((new Date(detail.completed_at).getTime() - new Date(detail.created_at).getTime()) / 1000)
      : null,
  );

  const statusIcon: Record<string, string> = {
    running: "◎", completed: "✓", failed: "✗", queued: "○", cancelled: "◼",
  };

  const statusMuted = detail && !statusIcon[detail.status];

  return (
    <div className="subagent-detail-overlay">
      {/* Header */}
      <div className="subagent-detail-header">
        <button type="button" onClick={onClose} className="subagent-detail-header-btn">
          ← Back
        </button>
        <span className="subagent-detail-header-info">
          {detail ? (
            <>
              <span className="subagent-detail-header-accent">{detail.agent_name}</span>
              {" · "}
              <span className={statusMuted ? "subagent-detail-header-status muted" : "subagent-detail-header-status"}>
                {statusIcon[detail.status] || "●"} {detail.status}
              </span>
              {" · "}
              <span>{childSessionId.slice(0, 8)}</span>
            </>
          ) : (
            childSessionId.slice(0, 8)
          )}
        </span>
        {!!(detail as { metadata?: { worktree_path?: unknown } } | undefined)?.metadata?.worktree_path && (
          <span className="subagent-detail-header-badge">Worktree</span>
        )}
      </div>

      {/* Summary stats card */}
      {detail && !loading && (
        <div className="subagent-summary-bar">
          <div className="subagent-summary-stat">
            <span className="subagent-summary-stat-label">Duration</span>
            <span className="subagent-summary-stat-value">{durationLabel}</span>
          </div>
          <div className="subagent-summary-stat">
            <span className="subagent-summary-stat-label">Events</span>
            <span className="subagent-summary-stat-value">{events.length}</span>
          </div>
          <div className="subagent-summary-stat">
            <span className="subagent-summary-stat-label">Actions</span>
            <span className="subagent-summary-stat-value">{toolCalls}</span>
          </div>
          <div className="subagent-summary-stat">
            <span className="subagent-summary-stat-label">Results</span>
            <span className="subagent-summary-stat-value">{observations}</span>
          </div>
          <div className="subagent-summary-stat">
            <span className="subagent-summary-stat-label">Thoughts</span>
            <span className="subagent-summary-stat-value">{thoughts}</span>
          </div>
          {detail.total_tokens_estimate != null && (
            <div className="subagent-summary-stat">
              <span className="subagent-summary-stat-label">Tokens</span>
              <span className="subagent-summary-stat-value">
                {detail.total_tokens_estimate.toLocaleString()}
              </span>
            </div>
          )}
          {detail.message_count != null && (
            <div className="subagent-summary-stat">
              <span className="subagent-summary-stat-label">Messages</span>
              <span className="subagent-summary-stat-value">{detail.message_count}</span>
            </div>
          )}
        </div>
      )}

      {/* Event filter chips */}
      {!loading && events.length > 0 && (
        <div className="subagent-filter-row">
          {EVENT_FILTER_OPTIONS.map((opt) => {
            const count = opt.key === "all" ? events.length : countByType(events, opt.key);
            return (
              <button
                key={opt.key}
                type="button"
                className={`subagent-filter-chip ${eventFilter === opt.key ? "active" : ""}`}
                onClick={() => setEventFilter(opt.key)}
              >
                {opt.label}
                {count > 0 && <span className="subagent-filter-count">{count}</span>}
              </button>
            );
          })}
        </div>
      )}

      {/* Timeline */}
      <div className="subagent-detail-body">
        {loading ? (
          <div className="subagent-detail-empty">Loading subagent log…</div>
        ) : loadError ? (
          <div className="subagent-detail-empty-error">
            Failed to load subagent data.{" "}
            <button
              type="button"
              onClick={() => { setLoading(true); setLoadError(false); setRetryKey((k) => k + 1); }}
              className="subagent-detail-retry-btn"
            >
              Retry
            </button>
          </div>
        ) : events.length === 0 ? (
          <div className="subagent-detail-empty">No events recorded for this subagent.</div>
        ) : filteredEvents.length === 0 ? (
          <div className="subagent-detail-empty">No events match the selected filter.</div>
        ) : (
          filteredEvents.map((ev, i) => (
            <WsEventBlock
              key={`${ev.type}-${(ev as { timestamp?: string }).timestamp || i}`}
              event={ev}
            />
          ))
        )}
      </div>

      {/* Worktree actions footer */}
      {hasWorktree && !isResolved && !isFailed && (
        <div className="subagent-detail-footer">
          <span className="subagent-detail-footer-msg">
            {worktreeAction ? `${worktreeAction}ing...` : "Worktree has unmerged changes"}
          </span>
          {!worktreeAction && (
            <>
              <button
                type="button"
                onClick={() => handleWorktree("apply")}
                className="subagent-detail-footer-btn subagent-detail-footer-btn-primary"
              >
                Apply Changes
              </button>
              <button
                type="button"
                onClick={() => handleWorktree("discard")}
                className="subagent-detail-footer-btn subagent-detail-footer-btn-danger"
              >
                Discard
              </button>
              <button
                type="button"
                onClick={() => handleWorktree("retain")}
                className="subagent-detail-footer-btn subagent-detail-footer-btn-default"
              >
                Retain
              </button>
            </>
          )}
        </div>
      )}
      {isResolved && (
        <div className="subagent-detail-status">
          {resolvedStatus === "applied" && "✓ Worktree applied — changes merged"}
          {resolvedStatus === "discarded" && "✓ Worktree discarded — changes removed"}
          {resolvedStatus === "retained" && "✓ Worktree retained — kept on disk"}
        </div>
      )}
      {isFailed && (
        <div className="subagent-detail-status-error">
          ✗ Worktree operation failed —{" "}
          <button
            type="button"
            onClick={() => { setWorktreeAction(null); }}
            className="subagent-detail-retry-btn"
          >
            retry
          </button>
        </div>
      )}
    </div>
  );
}
