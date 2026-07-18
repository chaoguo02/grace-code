/**
 * ToolApprovalCard — renders a pending tool approval in the timeline.
 *
 * CC equivalent: the permission prompt dialog shown when a tool needs
 * approval in TTY mode.  In headless Web mode this replaces the TTY
 * prompt with an inline card.
 */
import { useState } from "react";

interface ToolApprovalCardProps {
  requestId: string;
  toolName: string;
  params: Record<string, unknown>;
  thought?: string;
  onApprove: (note?: string) => void;
  onAlwaysAllow: (note?: string) => void;
  onDeny: (note?: string) => void;
  disabled?: boolean;
}

const TOOL_ICONS: Record<string, string> = {
  Read: "📖", Write: "✏️", Edit: "📝", Bash: "💻",
  Glob: "🔍", Grep: "🔎", Agent: "🤖", WebFetch: "🌐",
};

function toolIcon(name: string): string {
  return TOOL_ICONS[name] || "🔧";
}

function formatParams(params: Record<string, unknown>): string {
  const entries = Object.entries(params).slice(0, 3);
  return entries.map(([k, v]) => {
    const val = typeof v === "string" ? v.slice(0, 60) : JSON.stringify(v).slice(0, 60);
    return `${k}: ${val}`;
  }).join(", ");
}

export function ToolApprovalCard({
  requestId, toolName, params, thought,
  onApprove, onAlwaysAllow, onDeny, disabled,
}: ToolApprovalCardProps) {
  const [note, setNote] = useState("");

  return (
    <div className="message" style={{ marginBottom: 12 }}>
      <div className="message-row">
        <div className="message-avatar"
          style={{ background: "var(--accent-soft)", color: "var(--accent)", fontSize: 14 }}
        >
          {toolIcon(toolName)}
        </div>
        <div className="plan-card" style={{ flex: 1, margin: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--accent)", marginBottom: 6 }}>
            Approve tool: <code style={{ fontSize: 12 }}>{toolName}</code>
          </div>
          <div style={{ fontSize: 12, color: "var(--text-dim)", fontFamily: "var(--font-mono)", marginBottom: 6 }}>
            {formatParams(params)}
          </div>
          {thought && (
            <div style={{ fontSize: 11, color: "var(--text-muted)", fontStyle: "italic", marginBottom: 8, maxHeight: 60, overflow: "hidden" }}>
              {thought.slice(0, 200)}
            </div>
          )}
          <div style={{ fontSize: 10, color: "var(--text-muted)", marginBottom: 8 }}>
            ID: {requestId}
          </div>
          <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <input
              type="text"
              placeholder="Optional note…"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              disabled={disabled}
              style={{
                flex: 1, padding: "4px 8px", fontSize: 12,
                background: "var(--bg)", border: "1px solid var(--border)",
                borderRadius: "var(--radius-sm)", color: "var(--text)",
              }}
            />
            <button
              className="btn-approve"
              type="button"
              disabled={disabled}
              onClick={() => onApprove(note || undefined)}
              style={{ padding: "4px 12px", fontSize: 12 }}
            >
              Allow Once
            </button>
            <button
              className="btn-ghost"
              type="button"
              disabled={disabled}
              onClick={() => onAlwaysAllow(note || undefined)}
              style={{ padding: "4px 12px", fontSize: 12 }}
              title="Always allow this tool (persisted to settings.json)"
            >
              Always
            </button>
            <button
              className="btn-reject"
              type="button"
              disabled={disabled}
              onClick={() => onDeny(note || "Denied by user")}
              style={{ padding: "4px 12px", fontSize: 12 }}
            >
              Deny
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
