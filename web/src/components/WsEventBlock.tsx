import type { WsMessage } from "../types";

function iconFor(event: WsMessage): string {
  switch (event.type) {
    case "thought":
      return "◎";
    case "tool_call":
      return "⚙";
    case "observation":
      return event.status === "error" ? "!" : "✓";
    case "reflection":
      return "↺";
    case "subagent_start":
      return "⇢";
    case "subagent_stop":
      return "⇠";
    case "status":
      return "●";
    default:
      return "•";
  }
}

function titleFor(event: WsMessage): string {
  switch (event.type) {
    case "thought":
      return "Thought";
    case "tool_call":
      return event.name || "Tool Call";
    case "observation":
      return event.tool_name || "Observation";
    case "reflection":
      return "Reflection";
    case "subagent_start":
      return `Subagent ${event.agent_name || ""}`.trim();
    case "subagent_stop":
      return "Subagent finished";
    case "status":
      return event.status || "Status";
    default:
      return event.type || "Event";
  }
}

function bodyFor(event: WsMessage): string {
  if (event.type === "thought" || event.type === "reflection") return event.content || "";
  if (event.type === "tool_call") return JSON.stringify(event.params || {}, null, 2);
  if (event.type === "observation") return event.output || event.error || "";
  if (event.type === "subagent_start") return `Child session ${event.child_session_id || ""}`;
  if (event.type === "subagent_stop") return event.status || "";
  if (event.type === "status") return event.message || event.error || "";
  return JSON.stringify(event.payload || {}, null, 2);
}

function cardClass(event: WsMessage): string {
  switch (event.type) {
    case "thought":
      return "trace-card trace-thought";
    case "tool_call":
      return "trace-card trace-tool";
    case "observation":
      return "trace-card trace-observation";
    case "reflection":
      return "trace-card trace-reflection";
    default:
      return "trace-card";
  }
}

export function WsEventBlock({ event }: { event: WsMessage }) {
  if (event.type === "status" && !["finish", "gave_up"].includes(event.status || "")) {
    return null;
  }

  const body = bodyFor(event);

  return (
    <div className="trace-block">
      <div className={cardClass(event)}>
        <div className="trace-header">
          <div className="trace-icon">{iconFor(event)}</div>
          <div className="trace-title">{titleFor(event)}</div>
          <div className="trace-meta">
            {event.step != null && <span className="trace-pill">Step {event.step}</span>}
            {event.status && event.type !== "status" && (
              <span className="trace-pill">{event.status}</span>
            )}
          </div>
        </div>
        <div className="trace-content">
          {event.type === "tool_call" || event.type === "observation" ? (
            <pre>{body}</pre>
          ) : (
            body
          )}
        </div>
        {event.diff && event.type === "observation" && (
          <details className="trace-diff" style={{ marginTop: 8, borderTop: "1px solid var(--border)", paddingTop: 8 }}>
            <summary style={{ cursor: "pointer", fontSize: 12, fontWeight: 600, color: "var(--accent)", userSelect: "none" }}>
              View Diff ({event.diff.split("\n").length} lines)
            </summary>
            <pre style={{ fontSize: 11, lineHeight: 1.4, marginTop: 6, background: "var(--code-bg)", padding: 8, borderRadius: 6, overflowX: "auto" }}>{event.diff}</pre>
          </details>
        )}
      </div>
    </div>
  );
}
