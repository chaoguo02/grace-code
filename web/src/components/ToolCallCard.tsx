import { useState } from "react";
import type { WsMessage } from "../types";

const TOOL_ICONS: Record<string, string> = {
  Read: "R",
  Write: "W",
  Edit: "E",
  Bash: "B",
  Glob: "G",
  Grep: "S",
  Agent: "A",
  task: "A",
  WebFetch: "F",
  WebSearch: "Q",
};

function getToolIcon(name: string): string {
  return TOOL_ICONS[name] || "T";
}

function formatJson(obj: Record<string, unknown> | string, maxLen = 200): string {
  const s = typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
  return s.length > maxLen ? `${s.slice(0, maxLen)}…` : s;
}

function summarizeTarget(params: Record<string, unknown>) {
  const keys = ["file_path", "path", "target_file", "command", "pattern", "url"];
  for (const key of keys) {
    const value = params[key];
    if (value != null) return String(value);
  }
  return "No explicit target";
}

interface ToolCallCardProps {
  name: string;
  params: Record<string, unknown>;
  id?: string;
  step?: number;
  observation?: WsMessage | null;
  className?: string;
}

export function ToolCallCard({ name, params, id, step, observation, className }: ToolCallCardProps) {
  const [expanded, setExpanded] = useState(false);
  const paramsStr = formatJson(params, expanded ? Infinity : 220);
  const summary = summarizeTarget(params);
  const obs = observation as { output?: string; error?: string; status?: string; tool_name?: string } | null;
  const observationPreview = (obs?.output || obs?.error || "").replace(/\s+/g, " ").slice(0, 160);

  return (
    <div
      className={`tool-call-card timeline-action-card paired-run-card${className ? ` ${className}` : ""}${observation ? " has-observation" : ""}${obs?.status === "error" ? " observation-error" : ""}`}
      onClick={() => setExpanded((v) => !v)}
      title="Click to expand or collapse"
    >
      <div className="timeline-card-topline">
        <span className="timeline-card-label">Action</span>
        <span className="timeline-card-minor">{step != null ? `Step ${step}` : "Tool"}</span>
      </div>

      <div className="tc-header">
        <span className="tc-icon">{getToolIcon(name)}</span>
        <div className="timeline-card-heading-group">
          <span className="tc-name">{escapeHtml(name)}</span>
          <span className="timeline-card-subtitle">{escapeHtml(summary)}</span>
        </div>
        {id && <span className="tc-id" title={id}>{id.slice(0, 8)}</span>}
        <button type="button" className="trace-expand-btn tc-expand-btn">
          {expanded ? "Hide" : "Details"}
        </button>
      </div>

      <div className="paired-run-flow">
        <div className="paired-run-stage paired-run-stage-action">
          <div className="paired-run-stage-label">Input</div>
          <pre className="tc-params" style={{ maxHeight: expanded ? "none" : "92px" }}>
            {escapeHtml(paramsStr)}
          </pre>
        </div>

        {observation && (
          <div className={`paired-run-stage paired-run-stage-observation ${obs?.status === "error" ? "error" : "success"}`}>
            <div className="paired-run-stage-label">Result</div>
            <div className="tc-observation-summary">
              <span className="obs-status-icon">{obs?.status === "error" ? "!" : "OK"}</span>
              <span className="obs-tool-name">{escapeHtml(obs?.tool_name || name)}</span>
              <span className="obs-output-preview">{escapeHtml(observationPreview)}</span>
            </div>
            {expanded ? (
              <pre className="paired-run-observation-detail">
                {escapeHtml(observation.output || observation.error || "")}
              </pre>
            ) : null}
          </div>
        )}
      </div>
    </div>
  );
}

function escapeHtml(s: string) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
