/**
 * SubagentProgress — floating progress card for background subagents.
 *
 * CC-aligned: shows animated spinner, tool count, token usage,
 * and the subagent's last action description.  Appears in the
 * bottom-right corner of ChatView.  Auto-dismisses on completion.
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
    <div style={{
      position: "fixed", bottom: 80, right: 20, zIndex: 100,
      display: "flex", flexDirection: "column", gap: 8,
    }}>
      {visible.map((agent) => (
        <div key={agent.childSessionId}
          style={{
            background: "var(--bg-elev)",
            border: "1px solid var(--border)",
            borderRadius: 8,
            padding: "10px 14px",
            fontSize: 12,
            minWidth: 240,
            boxShadow: "0 2px 8px rgba(0,0,0,0.15)",
            opacity: agent.status !== "running" ? 0.7 : 1,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
            {agent.status === "running" ? (
              <span style={{ color: "var(--accent)", animation: "spin 1s linear infinite" }}>◎</span>
            ) : agent.status === "completed" ? (
              <span style={{ color: "var(--green, #4caf50)" }}>✓</span>
            ) : (
              <span style={{ color: "var(--red, #f44336)" }}>✗</span>
            )}
            <span style={{ fontWeight: 600 }}>{agent.agentName}</span>
            <span style={{ color: "var(--text-muted)", fontSize: 10 }}>
              {agent.toolCount} tools
            </span>
          </div>
          {agent.lastAction && (
            <div style={{
              color: "var(--text-muted)", fontSize: 10,
              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
            }}>
              {agent.lastAction}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
