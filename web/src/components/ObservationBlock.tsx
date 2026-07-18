import { useState } from "react";

interface ObservationBlockProps {
  tool_name: string;
  output: string;
  status?: string;
  error?: string | null;
  id?: string | null;
  step?: number;
  className?: string;
}

function escapeHtml(s: string): string {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

export function ObservationBlock({
  tool_name,
  output,
  status,
  error,
  id,
  step,
  className,
}: ObservationBlockProps) {
  const [expanded, setExpanded] = useState(false);
  const isError = status === "error" || status === "timeout";
  const text = isError ? (error || output) : output;
  const displayText = expanded ? text : text.slice(0, 300);

  return (
    <div
      className={`observation-block${className ? " " + className : ""}${isError ? " error" : " success"}`}
      style={{ cursor: "pointer" }}
      onClick={() => setExpanded(!expanded)}
      title="Click to expand/collapse"
    >
      <div className="obs-header">
        <span className="obs-status-icon">{isError ? "⚠" : "✓"}</span>
        <span className="obs-tool-name">{escapeHtml(tool_name)}</span>
        {status && <span className="obs-status-tag">{status}</span>}
        {step != null && <span className="obs-step">Step {step}</span>}
        {id && <span className="obs-id" title={id}>{id.slice(0, 8)}</span>}
        {text.length > 300 && (
          <span className="obs-expand">{expanded ? "▲" : "▼ more"}</span>
        )}
      </div>
      <pre className="obs-output" style={{ maxHeight: expanded ? "none" : "80px" }}>
        {escapeHtml(displayText)}
        {!expanded && text.length > 300 && "…"}
      </pre>
    </div>
  );
}
