import type { WsMessage } from "../types";
import { ToolCallCard } from "./ToolCallCard";
import { ObservationBlock } from "./ObservationBlock";

function escapeHtml(s: string): string {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** Renders a live WS event as a timeline block. */
export function WsEventBlock({ event }: { event: WsMessage }) {
  switch (event.type) {
    case "thought":
      return (
        <div className="message assistant">
          <div className="message-row">
            <div className="message-avatar" style={{ background: "var(--bg-soft)", color: "var(--text-dim)", fontSize: 10 }}>🤔</div>
            <div className="message-bubble" style={{ opacity: 0.75, fontStyle: "italic" }}>
              {escapeHtml(event.content || "")}
            </div>
          </div>
        </div>
      );

    case "tool_call":
      return (
        <ToolCallCard
          name={event.name || ""}
          params={event.params || {}}
          id={event.id}
          step={event.step}
        />
      );

    case "observation":
      return (
        <ObservationBlock
          tool_name={event.tool_name || ""}
          output={event.output || ""}
          status={event.status}
          error={event.error}
          id={event.id}
          step={event.step}
        />
      );

    case "reflection":
      return (
        <div className="message assistant">
          <div className="message-row">
            <div className="message-avatar" style={{ background: "var(--bg-soft)", color: "var(--text-dim)", fontSize: 10 }}>💭</div>
            <div className="message-bubble" style={{ opacity: 0.6, fontStyle: "italic", fontSize: 13 }}>
              {escapeHtml(event.content || "")}
            </div>
          </div>
        </div>
      );

    case "subagent_start":
      return (
        <div className="message" style={{ marginBottom: 4 }}>
          <div className="message-row">
            <div className="message-avatar" style={{ background: "var(--accent-soft)", color: "var(--accent)", fontSize: 10 }}>⊞</div>
            <div className="message-bubble" style={{ fontSize: 12, color: "var(--text-dim)" }}>
              Subagent <strong>{escapeHtml(event.agent_name || "")}</strong> started ({escapeHtml(event.child_session_id || "").slice(0, 8)})
            </div>
          </div>
        </div>
      );

    case "subagent_stop":
      return (
        <div className="message" style={{ marginBottom: 4 }}>
          <div className="message-row">
            <div className="message-avatar" style={{ background: "var(--bg-soft)", color: "var(--text-dim)", fontSize: 10 }}>⊟</div>
            <div className="message-bubble" style={{ fontSize: 12, color: "var(--text-dim)" }}>
              Subagent completed: {escapeHtml(event.status || "")}
            </div>
          </div>
        </div>
      );

    case "plan_ready":
      return (
        <div className="plan-card">
          <h2>📋 Plan Ready for Review</h2>
          <div
            style={{
              maxHeight: 300,
              overflow: "auto",
              whiteSpace: "pre-wrap",
              fontSize: 13,
              color: "var(--text)",
              lineHeight: 1.6,
            }}
            dangerouslySetInnerHTML={{
              __html: (event.plan_text || "").replace(/\n/g, "<br>"),
            }}
          />
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 8 }}>
            {event.result?.steps_taken != null && `Steps: ${event.result.steps_taken} · `}
            {event.result?.total_tokens != null && `Tokens: ${event.result.total_tokens}`}
          </div>
        </div>
      );

    case "status":
      if (event.status === "finish" || event.status === "gave_up") {
        return (
          <div className="message assistant">
            <div className="message-row">
              <div className="message-avatar" style={{ background: "var(--success-soft)", color: "var(--success)" }}>✓</div>
              <div className="message-bubble">{escapeHtml(event.message || "")}</div>
            </div>
          </div>
        );
      }
      return null;

    default:
      return null;
  }
}
