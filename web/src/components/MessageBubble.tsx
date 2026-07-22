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
    const isError = /^(Error|Traceback|Fatal|Exception|FAILED)\b/im.test(message.content.trimStart());
    return (
      <div className="message tool">
        <div className="message-row">
          <div className="message-avatar">{isError ? "!" : "OK"}</div>
          <div className={`observation-block ${isError ? "error" : "success"}`} style={{ flex: 1 }}>
            <div className="obs-header">
              {tcId && <span className="obs-id" title={tcId}>{tcId.slice(0, 8)}</span>}
              <span className="obs-status-tag">{isError ? "error" : "success"}</span>
            </div>
            <pre className="obs-output" style={{ maxHeight: 320, overflow: "auto" }}>
              {message.content}
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
        const obsContent = tc.id ? toolResults?.get(tc.id) : undefined;
        const obsError = obsContent != null
          ? /^(Error|Traceback|Fatal|Exception|FAILED)\b/i.test(obsContent.trimStart())
          : false;
        return (
          <ToolCallCard
            key={tc.id || i}
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
