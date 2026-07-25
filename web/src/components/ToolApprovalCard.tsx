/**
 * Inline HITL approval bar — CC-aligned permission prompt.
 *
 * Renders as a compact bar above the composer, not a floating card.
 * Keyboard: Y=approve, N=deny, Shift+Y=approve+remember.
 * Focus safety: keys are disabled when user is typing in input/textarea.
 */
import { useState, useEffect, useCallback } from "react";

interface ToolApprovalCardProps {
  requestId: string;
  toolName: string;
  params: Record<string, unknown>;
  thought?: string;
  decisionReason?: string;
  toolUseId?: string;
  permissionMode?: string;
  riskLevel?: string;
  onApprove: (note?: string) => void;
  onAlwaysAllow: (note?: string) => void;
  onDeny: (note?: string) => void;
  disabled?: boolean;
}

type MemoryScope = "once" | "session" | "file_pattern";

function summarizeTarget(params: Record<string, unknown>): string {
  const priorityKeys = ["file_path", "path", "target_file", "command", "pattern", "url"];
  for (const key of priorityKeys) {
    const v = params[key];
    if (v != null) {
      const s = String(v);
      return s.length > 100 ? s.slice(0, 97) + "…" : s;
    }
  }
  return "";
}

function riskColor(riskLevel?: string): string {
  if (riskLevel === "high") return "var(--error)";
  if (riskLevel === "medium") return "var(--warning)";
  return "var(--text-muted)";
}

export function ToolApprovalCard({
  requestId, toolName, params, thought, decisionReason, riskLevel,
  onApprove, onAlwaysAllow, onDeny, disabled,
}: ToolApprovalCardProps) {
  const [scope, setScope] = useState<MemoryScope>("once");
  const target = summarizeTarget(params);
  const risk = riskLevel || "medium";

  const approve = useCallback(() => {
    if (scope === "session") onAlwaysAllow();
    else onApprove();
  }, [scope, onApprove, onAlwaysAllow]);

  const deny = useCallback(() => onDeny(), [onDeny]);

  // Global keyboard shortcuts with focus safety
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName?.toLowerCase();
      const isEditing = tag === "input" || tag === "textarea" ||
        (e.target as HTMLElement)?.getAttribute("contenteditable") === "true";
      if (isEditing) return;

      if (e.key === "y" || e.key === "Y") {
        e.preventDefault();
        if (e.shiftKey) { setScope("session"); onAlwaysAllow(); }
        else onApprove();
      }
      if (e.key === "n" || e.key === "N") {
        e.preventDefault();
        onDeny();
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onApprove, onAlwaysAllow, onDeny]);

  return (
    <div
      className="hitl-bar"
      style={{ borderLeftColor: riskColor(riskLevel) }}
      role="alert"
      aria-live="polite"
    >
      <div className="hitl-main-row">
        <span className="hitl-icon" title={riskLevel || "medium"}>
          {risk === "high" ? "⚠" : "⚡"}
        </span>
        <span className="hitl-tool-name">{toolName}</span>
        {target && <span className="hitl-target">: {target}</span>}
        {thought && <span className="hitl-thought" title={thought}>{thought.slice(0, 60)}{thought.length > 60 ? "…" : ""}</span>}

        <span className="hitl-spacer" />

        <span className="hitl-scope-label">Remember:</span>
        <select
          className="hitl-scope-select"
          value={scope}
          onChange={(e) => setScope(e.target.value as MemoryScope)}
          disabled={disabled}
        >
          <option value="once">Once</option>
          <option value="session">Session</option>
          <option value="file_pattern" disabled title="Backend support pending (P2)">File Pattern ⏳</option>
        </select>

        <button className="hitl-btn hitl-deny" type="button" disabled={disabled} onClick={deny}>
          <kbd>N</kbd> Deny
        </button>
        <button className="hitl-btn hitl-approve" type="button" disabled={disabled} onClick={approve}>
          <kbd>Y</kbd> Approve
        </button>
      </div>

      {decisionReason && (
        <div className="hitl-reason">{decisionReason}</div>
      )}
    </div>
  );
}
