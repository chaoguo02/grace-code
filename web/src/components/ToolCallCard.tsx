import { useState } from "react";
import type { ToolCall, WsMessage } from "../types";

const TOOL_ICONS: Record<string, string> = {
  Read: "📖",
  Write: "✏️",
  Edit: "📝",
  Bash: "💻",
  Glob: "🔍",
  Grep: "🔎",
  Agent: "🤖",
  task: "🤖",
  WebFetch: "🌐",
  WebSearch: "🔎",
};

function getToolIcon(name: string): string {
  return TOOL_ICONS[name] || "🔧";
}

function formatJson(obj: Record<string, unknown> | string, maxLen = 200): string {
  const s = typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
  return s.length > maxLen ? s.slice(0, maxLen) + "…" : s;
}

interface ToolCallCardProps {
  /** Tool call data (from persisted message or WS event) */
  name: string;
  params: Record<string, unknown>;
  id?: string;
  step?: number;
  /** Paired observation (matched by id) */
  observation?: WsMessage | null;
  /** Extra class */
  className?: string;
}

export function ToolCallCard({ name, params, id, step, observation, className }: ToolCallCardProps) {
  const [expanded, setExpanded] = useState(false);
  const paramsStr = formatJson(params, expanded ? Infinity : 200);

  return (
    <div
      className={`tool-call-card${className ? " " + className : ""}${observation ? " has-observation" : ""}${observation?.status === "error" ? " observation-error" : ""}`}
      style={{ cursor: "pointer" }}
      onClick={() => setExpanded(!expanded)}
      title="Click to expand/collapse"
    >
      <div className="tc-header">
        <span className="tc-icon">{getToolIcon(name)}</span>
        <span className="tc-name">{escapeHtml(name)}</span>
        {id && <span className="tc-id" title={id}>{id.slice(0, 8)}</span>}
        {step != null && <span className="tc-step">Step {step}</span>}
        <span className="tc-expand">{expanded ? "▲" : "▼"}</span>
      </div>
      <pre className="tc-params" style={{ maxHeight: expanded ? "none" : "80px" }}>
        {escapeHtml(paramsStr)}
      </pre>

      {observation && (
        <div className={`tc-observation-summary${observation.status === "error" ? " error" : " success"}`}>
          <span className="obs-status-icon">{observation.status === "error" ? "⚠" : "✓"}</span>
          <span className="obs-tool-name">{escapeHtml(observation.tool_name || name)}</span>
          <span className="obs-output-preview">
            {escapeHtml((observation.output || observation.error || "").slice(0, 120))}
          </span>
        </div>
      )}
    </div>
  );
}

function escapeHtml(s: string): string {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
