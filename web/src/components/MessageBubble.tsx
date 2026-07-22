import type { Message } from "../types";
import { ToolCallCard } from "./ToolCallCard";
import { MarkdownRenderer } from "./MarkdownRenderer";

interface Props {
  message: Message;
  toolResults?: Map<string, string>;
}

export function MessageBubble({ message, toolResults }: Props) {
  const avatar =
    message.role === "user" ? "U" : message.role === "assistant" ? "GC" : "T";

  if (message.role === "tool") {
    const tcId = message.tool_call_id || null;
    const isError = message.content.toLowerCase().includes("error");
    return (
      <div className="message tool">
        <div className="message-row">
          <div className="message-avatar">{isError ? "!" : "OK"}</div>
          <div className={`observation-block ${isError ? "error" : "success"}`} style={{ flex: 1 }}>
            <div className="obs-header">
              {tcId && <span className="obs-id" title={tcId}>{tcId.slice(0, 8)}</span>}
              <span className="obs-status-tag">{isError ? "error" : "success"}</span>
            </div>
            <pre className="obs-output" style={{ maxHeight: 200, overflow: "auto" }}>
              {message.content.slice(0, 500)}
            </pre>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={`message ${message.role} timeline-message timeline-message-${message.role}`}>
      <div className="message-row">
        <div className="message-avatar">{avatar}</div>
        <div className="timeline-message-main">
          <div className="timeline-card-topline">
            <span className="timeline-card-label">
              {message.role === "user" ? "Prompt" : "Final answer"}
            </span>
          </div>
          <MarkdownRenderer
            className={`message-bubble ${message.role === "assistant" ? "message-bubble-final" : "message-bubble-prompt"}`}
            content={message.content}
          />
        </div>
      </div>
      {message.tool_calls?.map((tc, i) => {
        const obsContent = toolResults?.get(tc.id || "");
        const obsError = obsContent?.toLowerCase().includes("error");
        return (
          <ToolCallCard
            key={i}
            name={tc.name}
            params={tc.params}
            id={tc.id}
            observation={
              obsContent != null
                ? {
                    type: "observation",
                    tool_name: tc.name,
                    output: obsContent,
                    status: obsError ? "error" : "success",
                  }
                : undefined
            }
          />
        );
      })}
    </div>
  );
}
