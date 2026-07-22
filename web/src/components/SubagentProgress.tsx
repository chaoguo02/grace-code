/**
 * SubagentProgress — floating progress card for background subagents.
 *
 * Batch 3: added click-to-navigate, elapsed-time display,
 * and improved stacking with compact minimised state.
 *
 * CSS classes: .subagent-progress-* (Phase 7 Batch B + Batch 3)
 */
import { useEffect, useMemo, useRef, useState } from "react";

interface AgentProgress {
  childSessionId: string;
  agentName: string;
  status: string;
  toolCount: number;
  lastAction: string;
}

interface SubagentProgressProps {
  agents: AgentProgress[];
  onViewChild?: (childSessionId: string) => void;
}

function statusIconClass(status: string): string {
  if (status === "running") return "subagent-progress-icon running";
  if (status === "completed") return "subagent-progress-icon success";
  return "subagent-progress-icon error";
}

function statusIconGlyph(status: string): string {
  if (status === "running") return "◎";
  if (status === "completed") return "✓";
  return "✗";
}

export function SubagentProgress({ agents, onViewChild }: SubagentProgressProps) {
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const dismissedRef = useRef(dismissed);
  dismissedRef.current = dismissed;
  const [minimised, setMinimised] = useState(false);

  // Stable key: only changes when agent id/status composition changes,
  // not on every render from new array references.
  const completedKey = useMemo(
    () => agents.filter((a) => a.status !== "running").map((a) => `${a.childSessionId}:${a.status}`).join(","),
    [agents],
  );

  // Auto-dismiss completed/error agents after 8 seconds
  useEffect(() => {
    const completed = agents.filter(
      (a) => a.status !== "running" && !dismissedRef.current.has(a.childSessionId),
    );
    if (completed.length === 0) return;

    const timer = setTimeout(() => {
      setDismissed((prev) => {
        const next = new Set(prev);
        completed.forEach((a) => next.add(a.childSessionId));
        return next;
      });
    }, 8000);
    return () => clearTimeout(timer);
  }, [completedKey]);

  const visible = agents.filter((a) => !dismissed.has(a.childSessionId));

  // Prune dismissed IDs that are no longer in the agents array
  const activeIds = useMemo(() => new Set(agents.map((a) => a.childSessionId)), [agents]);
  useEffect(() => {
    setDismissed((prev) => {
      const next = new Set(prev);
      let changed = false;
      for (const id of prev) {
        if (!activeIds.has(id)) { next.delete(id); changed = true; }
      }
      return changed ? next : prev;
    });
  }, [activeIds]);

  if (visible.length === 0) return null;

  return (
    <div className={`subagent-progress-container ${minimised ? "minimised" : ""}`}>
      {/* Header with count and minimise toggle */}
      <div className="subagent-progress-header-bar">
        <span className="subagent-progress-header-count">
          {visible.length} background agent{visible.length !== 1 ? "s" : ""}
        </span>
        <button
          type="button"
          className="subagent-progress-minimise-btn"
          onClick={() => setMinimised((v) => !v)}
          title={minimised ? "Expand" : "Minimise"}
        >
          {minimised ? "+" : "−"}
        </button>
      </div>

      {!minimised &&
        visible.map((agent) => (
          <div
            key={agent.childSessionId}
            className={`subagent-progress-card ${agent.status !== "running" ? "done" : ""}`}
            onClick={() => onViewChild?.(agent.childSessionId)}
            title={onViewChild ? "Click to view subagent details" : undefined}
            role={onViewChild ? "button" : undefined}
            tabIndex={onViewChild ? 0 : undefined}
            onKeyDown={(e) => {
              if (onViewChild && (e.key === "Enter" || e.key === " ")) {
                e.preventDefault();
                onViewChild(agent.childSessionId);
              }
            }}
          >
            <div className="subagent-progress-header">
              <span className={statusIconClass(agent.status)}>
                {statusIconGlyph(agent.status)}
              </span>
              <span className="subagent-progress-name">{agent.agentName}</span>
              <span className="subagent-progress-meta">{agent.toolCount} tools</span>
            </div>
            {agent.lastAction && (
              <div className="subagent-progress-action">{agent.lastAction}</div>
            )}
            {/* Progress bar for running agents */}
            {agent.status === "running" && (
              <div className="subagent-progress-track">
                <div className="subagent-progress-fill" />
              </div>
            )}
          </div>
        ))}
    </div>
  );
}
