/**
 * SubagentDetail — child session execution log viewer.
 *
 * CC-aligned: shows full timeline of a subagent session with
 * back-to-parent navigation.  Opens when user clicks a child
 * session in SessionTree or a subagent_stop event in the timeline.
 *
 * CSS classes defined in styles.css: .subagent-detail-*  (Phase 7 Batch B)
 */
import { useEffect, useState } from "react";
import { useSessionStore } from "../stores/sessionStore";
import { selectSessionUi, useChatStore } from "../stores/chatStore";
import { WsEventBlock } from "./WsEventBlock";
import * as api from "../api/sessions";
import type { WsMessage, SessionDetail } from "../types";

interface SubagentDetailProps {
  childSessionId: string;
  onClose: () => void;
}

export function SubagentDetail({ childSessionId, onClose }: SubagentDetailProps) {
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [events, setEvents] = useState<WsMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [retryKey, setRetryKey] = useState(0);
  const [worktreeAction, setWorktreeAction] = useState<string | null>(null);
  const activeId = useSessionStore((s) => s.activeId);
  // Subscribe to WS-pushed worktree resolution state
  const worktreeStates = useChatStore((s) => selectSessionUi(s, activeId).worktreeStates);

  async function handleWorktree(action: string) {
    if (!activeId) return;
    setWorktreeAction(action);  // show spinner
    try {
      await fetch(
        `/api/sessions/${encodeURIComponent(activeId)}/worktrees/${encodeURIComponent(childSessionId)}/${action}`,
        { method: "POST" }
      );
      // Don't set isResolved here — wait for WS event.
      // The worker thread will push worktree_resolved when done.
    } catch {
      setWorktreeAction(null);  // network error → reset
    }
  }

  // Check WorktreeDisposition from session detail — the authoritative source.
  // metadata.worktree_path may persist after resolution.
  const hasWorktree = detail?.worktree_disposition === "preserved";
  const resolvedStatus = worktreeStates[`${childSessionId}_${worktreeAction}`]
    || worktreeStates[`${childSessionId}_apply`]
    || worktreeStates[`${childSessionId}_discard`]
    || worktreeStates[`${childSessionId}_retain`];
  const isResolved = resolvedStatus === "applied" || resolvedStatus === "discarded" || resolvedStatus === "retained";
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
              <span className="subagent-detail-header-accent">
                {detail.agent_name}
              </span>
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

      {/* Timeline */}
      <div className="subagent-detail-body">
        {loading ? (
          <div className="subagent-detail-empty">Loading subagent log…</div>
        ) : loadError ? (
          <div className="subagent-detail-empty-error">
            Failed to load subagent data.{" "}
            <button type="button" onClick={() => { setLoading(true); setLoadError(false); setRetryKey(k => k + 1); }}
              className="subagent-detail-retry-btn">Retry</button>
          </div>
        ) : events.length === 0 ? (
          <div className="subagent-detail-empty">No events recorded for this subagent.</div>
        ) : (
          events.map((ev, i) => (
            <WsEventBlock key={i} event={ev} />
          ))
        )}
      </div>

      {/* Worktree actions footer */}
      {hasWorktree && !isResolved && !isFailed && (
        <div className="subagent-detail-footer">
          <span className="subagent-detail-footer-msg">
            {worktreeAction ? `${worktreeAction}ing...` : "Worktree has unmerged changes"}
          </span>
          {!worktreeAction && (<>
            <button type="button" onClick={() => handleWorktree("apply")}
              className="subagent-detail-footer-btn subagent-detail-footer-btn-primary">
              Apply Changes
            </button>
            <button type="button" onClick={() => handleWorktree("discard")}
              className="subagent-detail-footer-btn subagent-detail-footer-btn-danger">
              Discard
            </button>
            <button type="button" onClick={() => handleWorktree("retain")}
              className="subagent-detail-footer-btn subagent-detail-footer-btn-default">
              Retain
            </button>
          </>)}
        </div>
      )}
      {isResolved && (
        <div className="subagent-detail-status">
          ✓ Worktree {resolvedStatus} — changes merged
        </div>
      )}
      {isFailed && (
        <div className="subagent-detail-status-error">
          ✗ Worktree operation failed —{" "}
          <button type="button" onClick={() => { setWorktreeAction(null); }}
            className="subagent-detail-retry-btn">retry</button>
        </div>
      )}
    </div>
  );
}
