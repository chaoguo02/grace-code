import type { Message, ToolCall } from "../types";
import { ToolCallCard } from "./ToolCallCard";

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function renderMarkdown(md: string): string {
  if (!md) return "";
  const codeBlocks: { lang: string; body: string }[] = [];
  let text = md.replace(
    /```(\w*)\n([\s\S]*?)```/g,
    (_: string, lang: string, body: string) => {
      const idx = codeBlocks.length;
      codeBlocks.push({ lang, body });
      return `\0CODE${idx}\0`;
    }
  );
  text = escapeHtml(text);
  text = text.replace(/^### (.+)$/gm, "<h3>$1</h3>");
  text = text.replace(/^## (.+)$/gm, "<h2>$1</h2>");
  text = text.replace(/^# (.+)$/gm, "<h1>$1</h1>");
  text = text.replace(/> (.+)$/gm, "<blockquote>$1</blockquote>");
  text = text.replace(/\*\*(\S.*?\S)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/\*(\S.*?\S)\*/g, "<em>$1</em>");
  text = text.replace(/`([^`]+)`/g, "<code>$1</code>");
  text = text.replace(/\n/g, "<br>");
  text = text.replace(/\0CODE(\d+)\0/g, (_: string, idx: string) => {
    const cb = codeBlocks[+idx];
    return `<pre><code>${escapeHtml(cb.body)}</code></pre>`;
  });
  return text;
}

interface Props {
  message: Message;
  /** Optional map of tool_call_id → result content for pairing with tool messages */
  toolResults?: Map<string, string>;
}

export function MessageBubble({ message, toolResults }: Props) {
  const avatar =
    message.role === "user" ? "U" : message.role === "assistant" ? "GC" : "T";

  // Tool result messages: show paired observation
  if (message.role === "tool") {
    const tcId = message.tool_call_id || null;
    const isError = message.content.toLowerCase().includes("error");
    return (
      <div className="message tool">
        <div className="message-row">
          <div
            className="message-avatar"
            style={{
              background: isError ? "var(--error-soft)" : "var(--success-soft)",
              color: isError ? "var(--error)" : "var(--success)",
            }}
          >
            {isError ? "⚠" : "✓"}
          </div>
          <div
            className="observation-block"
            style={{ flex: 1 }}
          >
            <div className="obs-header">
              {tcId && <span className="obs-id" title={tcId}>{tcId.slice(0, 8)}</span>}
              <span className="obs-status-tag">{isError ? "error" : "success"}</span>
            </div>
            <pre className="obs-output" style={{ maxHeight: 200, overflow: "auto" }}>
              {escapeHtml(message.content.slice(0, 500))}
            </pre>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={`message ${message.role}`}>
      <div className="message-row">
        <div className="message-avatar">{avatar}</div>
        <div
          className="message-bubble"
          dangerouslySetInnerHTML={{
            __html: renderMarkdown(message.content || ""),
          }}
        />
      </div>
      {message.tool_calls?.map((tc, i) => {
        // Look up paired result by tool_call_id in next messages
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
