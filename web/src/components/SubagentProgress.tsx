/**
 * SubagentProgress — floating progress card for background subagents.
 *
 * CC-aligned: shows animated spinner, tool count, token usage,
 * and the subagent's last action description.  Appears in the
 * bottom-right corner of ChatView.  Auto-dismisses on completion.
 *
 * CSS classes: .subagent-progress-* (Phase 7 Batch B)
 */
import { useEffect, useState } from "react";

interface AgentProgress {
  childSessionId: string;
  agentName: string;
  status: string;
  toolCount: number;
  lastAction: string;
}

interface SubagentProgressProps {
  agents: AgentProgress[];
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

export function SubagentProgress({ agents }: SubagentProgressProps) {
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());

  // Auto-dismiss completed agents after 5 seconds
  useEffect(() => {
    const completed = agents.filter(
      (a) => a.status !== "running" && !dismissed.has(a.childSessionId)
    );
    if (completed.length === 0) return;

    const timer = setTimeout(() => {
      setDismissed((prev) => {
        const next = new Set(prev);
        completed.forEach((a) => next.add(a.childSessionId));
        return next;
      });
    }, 5000);
    return () => clearTimeout(timer);
  }, [agents, dismissed]);

  const visible = agents.filter((a) => !dismissed.has(a.childSessionId));
  if (visible.length === 0) return null;

  return (
    <div className="subagent-progress-container">
      {visible.map((agent) => (
        <div key={agent.childSessionId}
          className={"subagent-progress-card" + (agent.status !== "running" ? " done" : "")}
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
        </div>
      ))}
    </div>
  );
}
