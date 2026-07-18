import { useState } from "react";

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

const TOOL_ICONS: Record<string, string> = {
  Read: "R",
  Write: "W",
  Edit: "E",
  Bash: "B",
  Glob: "G",
  Grep: "S",
  Agent: "A",
  WebFetch: "W",
};

function toolIcon(name: string): string {
  return TOOL_ICONS[name] || "T";
}

function formatValue(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function summarizeTarget(params: Record<string, unknown>) {
  const priorityKeys = ["file_path", "path", "target_file", "command", "pattern", "url"];
  for (const key of priorityKeys) {
    const value = params[key];
    if (value != null) return String(value);
  }
  const firstEntry = Object.entries(params)[0];
  if (!firstEntry) return "No explicit target";
  return `${firstEntry[0]}: ${formatValue(firstEntry[1])}`;
}

function inferRisk(toolName: string, params: Record<string, unknown>, riskLevel?: string) {
  if (riskLevel) return riskLevel;
  const lowered = `${toolName} ${JSON.stringify(params)}`.toLowerCase();
  if (toolName === "Write" || toolName === "Edit" || lowered.includes(".git") || lowered.includes("delete")) {
    return "high";
  }
  if (toolName === "Bash") return "medium";
  return "low";
}

function riskLabel(risk: string) {
  if (risk === "high") return "High risk";
  if (risk === "medium") return "Needs review";
  return "Low risk";
}

export function ToolApprovalCard({
  requestId,
  toolName,
  params,
  thought,
  decisionReason,
  toolUseId,
  permissionMode,
  riskLevel,
  onApprove,
  onAlwaysAllow,
  onDeny,
  disabled,
}: ToolApprovalCardProps) {
  const [note, setNote] = useState("");
  const risk = inferRisk(toolName, params, riskLevel);
  const paramEntries = Object.entries(params);
  const target = summarizeTarget(params);

  return (
    <div className="permission-card-wrap">
      <div className={`permission-card permission-risk-${risk}`}>
        <div className="permission-card-side" />
        <div className="permission-card-main">
          <div className="permission-card-header">
            <div className="permission-card-header-main">
              <div className="permission-card-icon">{toolIcon(toolName)}</div>
              <div>
                <div className="permission-card-eyebrow">Permission Request</div>
                <div className="permission-card-title">
                  Allow <code>{toolName}</code> to run?
                </div>
              </div>
            </div>
            <div className="permission-card-badges">
              <span className={`permission-badge risk-${risk}`}>{riskLabel(risk)}</span>
              <span className="permission-badge subtle">{permissionMode || "default"}</span>
            </div>
          </div>

          <div className="permission-hero-grid">
            <div className="permission-hero-panel">
              <div className="permission-panel-label">Target</div>
              <div className="permission-hero-value">{target}</div>
            </div>
            <div className="permission-hero-panel">
              <div className="permission-panel-label">Reason</div>
              <div className="permission-hero-value">
                {decisionReason || "This tool requires explicit user approval before execution."}
              </div>
            </div>
          </div>

          {thought ? (
            <div className="permission-section">
              <div className="permission-panel-label">Agent rationale</div>
              <div className="permission-rationale">{thought.slice(0, 300)}</div>
            </div>
          ) : null}

          {paramEntries.length ? (
            <div className="permission-section">
              <div className="permission-panel-label">Arguments</div>
              <div className="permission-args">
                {paramEntries.slice(0, 6).map(([key, value]) => (
                  <div key={key} className="permission-arg-row">
                    <span className="permission-arg-key">{key}</span>
                    <span className="permission-arg-value">{formatValue(value).slice(0, 140)}</span>
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          <div className="permission-meta-row">
            <span className="permission-meta-pill">Request ID: {requestId}</span>
            <span className="permission-meta-pill">Tool Use: {toolUseId || "pending"}</span>
          </div>

          <div className="permission-note-row">
            <input
              type="text"
              placeholder="Add an optional note for this approval..."
              value={note}
              onChange={(e) => setNote(e.target.value)}
              disabled={disabled}
              className="permission-note-input"
            />
          </div>

          <div className="permission-actions">
            <button
              className="btn-approve permission-primary-action"
              type="button"
              disabled={disabled}
              onClick={() => onApprove(note || undefined)}
            >
              Allow once
            </button>
            <button
              className="btn-ghost"
              type="button"
              disabled={disabled}
              onClick={() => onAlwaysAllow(note || undefined)}
              title="Persist an allow rule for matching future requests"
            >
              Always allow
            </button>
            <button
              className="btn-reject"
              type="button"
              disabled={disabled}
              onClick={() => onDeny(note || "Denied by user")}
            >
              Deny
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
