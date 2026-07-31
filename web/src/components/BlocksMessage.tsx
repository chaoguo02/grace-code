/**
 * BlocksMessage — renders an assistant message as ordered ContentBlocks.
 *
 * Replaces the old WsEventBlock + ToolCallCard + MessageBubble combo with
 * a single continuous flow: text → collapsed tool → text → collapsed thought.
 */
import { useState, useCallback } from "react";
import type { ContentBlock, ToolUseBlock, ThoughtBlock, TextBlock } from "../types/blocks";
import { toolUseSummary } from "../types/blocks";
import { MarkdownRenderer } from "./MarkdownRenderer";

// ── Props ──────────────────────────────────────────────────────────────────

interface BlocksMessageProps {
  blocks: ContentBlock[];
  role?: "user" | "assistant";
  metadata?: { steps: number; tokens: number; durationMs: number; agentName?: string; mode?: string };
}

// ── Tool icon ──────────────────────────────────────────────────────────────

function toolIcon(name: string): string {
  const icons: Record<string, string> = {
    Read: "📖", Write: "✏️", Edit: "✂️", Bash: ">_", Grep: "🔍",
    Glob: "📁", WebFetch: "🌐", WebSearch: "🔎", Agent: "🤖",
    Skill: "⚡", Task: "📋", SendMessage: "💬",
    git_status: "📋", git_diff: "📊", git_add: "➕", git_commit: "✅",
    memory_read: "🧠", memory_write: "📝", memory_list: "📚",
    EnterPlanMode: "◎", ExitPlanMode: "◉",
  };
  return icons[name] || "⚙";
}

function statusIcon(status: string): string {
  if (status === "running") return "⏳";
  if (status === "success") return "✓";
  if (status === "error") return "✗";
  return "";
}

function statusColor(status: string): string {
  if (status === "running") return "var(--accent)";
  if (status === "success") return "var(--ok, #2da44e)";
  if (status === "error") return "var(--error)";
  return "var(--text-muted)";
}

/** MIME-aware output rendering (P2). */
function renderOutput(output: string, toolName: string) {
  // JSON → collapsible tree
  if (toolName === "WebFetch" || (output.trim().startsWith("{") && output.trim().endsWith("}"))) {
    try {
      const obj = JSON.parse(output);
      return <pre className="output-json">{JSON.stringify(obj, null, 2)}</pre>;
    } catch { /* fall through */ }
  }
  // Large text → Markdown with collapse
  if (output.length > 1000) {
    return <MarkdownRenderer content={output.slice(0, 2000)} />;
  }
  return <MarkdownRenderer content={output} />;
}

// ── InlineToolBlock ────────────────────────────────────────────────────────

function InlineToolBlock({ block }: { block: ToolUseBlock }) {
  const [expanded, setExpanded] = useState(block.status === "error"); // errors default expanded
  const summary = toolUseSummary(block);
  const isMerged = block.groupedWith && block.groupedWith.length > 0;
  const isRetry = !!block.retryOf;

  const toggle = useCallback(() => setExpanded((v) => !v), []);

  return (
    <div
      className="inline-tool"
      style={{ borderLeftColor: statusColor(block.status) }}
    >
      <button
        type="button"
        className="inline-tool-header"
        onClick={toggle}
        aria-expanded={expanded}
      >
        <span className="inline-tool-status">{statusIcon(block.status)}</span>
        <span className="inline-tool-icon">{toolIcon(block.name)}</span>
        <span className="inline-tool-name">{block.name}</span>
        {isMerged && (
          <span className="inline-tool-group-count" title={`${block.groupLabel || block.name + " group"}\n${block.groupedWith!.length + 1} files merged`}>
            ({block.groupedWith!.length + 1})
          </span>
        )}
        {summary && <span className="inline-tool-summary">{summary}</span>}
        {isRetry && (
          <span className="inline-tool-retry">
            → Retried → {block.retrySucceeded ? "✓" : "✗"}
          </span>
        )}
        {block.evidence?.cached && (
          <span className="inline-tool-evidence-badge" title="Result served from a validated cache entry">
            cached
          </span>
        )}
        {block.evidence && (
          <span
            className="inline-tool-evidence-badge"
            title={`${block.evidence.kind} · ${block.evidence.evidence_id}${block.evidence.source_fingerprint ? ` · ${block.evidence.source_fingerprint}` : ""}`}
          >
            evidence {1 + (block.evidence.related_evidence_ids?.length || 0)}
          </span>
        )}
        <span className="inline-tool-chevron">{expanded ? "▲" : "▼"}</span>
      </button>

      {expanded && (
        <div className="inline-tool-body">
          {block.error && (
            <div className="inline-tool-error">⚠ {block.error}</div>
          )}
          {block.output && (
            <div className="inline-tool-output">
              {renderOutput(block.output, block.name)}
              {block.output.length > 2000 && (
                <div className="inline-tool-truncated">
                  … output truncated ({block.output.length.toLocaleString()} chars total)
                </div>
              )}
            </div>
          )}
          {!block.output && !block.error && block.status === "running" && (
            <div className="inline-tool-running">Waiting for result…</div>
          )}
          {/* Anchor targets for reference backlinks (P2) */}
          {block.anchorTargets && block.anchorTargets.length > 0 && (
            <div className="inline-tool-anchors">
              {block.anchorTargets.map((f) => (
                <span key={f} className="anchor-chip" title={`Referenced: ${f}`}>📎 {f}</span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── InlineThoughtBlock ─────────────────────────────────────────────────────

function InlineThoughtBlock({ block }: { block: ThoughtBlock }) {
  const [expanded, setExpanded] = useState(false);
  const isStreaming = block.phase === "streaming";
  const label = isStreaming
    ? "Thinking…"
    : block.summary || "Thought";

  const toggle = useCallback(() => setExpanded((v) => !v), []);

  return (
    <div className={`inline-thought${isStreaming ? " streaming" : ""}`}>
      <button
        type="button"
        className="inline-thought-header"
        onClick={toggle}
        aria-expanded={expanded}
      >
        <span className="inline-thought-dot" />
        <span className="inline-thought-label">{label}</span>
        <span className="inline-tool-chevron">{expanded ? "▲" : "▼"}</span>
      </button>
      {expanded && (
        <div className="inline-thought-body">
          {block.content}
        </div>
      )}
    </div>
  );
}

// ── BlocksMessage (main) ───────────────────────────────────────────────────

/** Merge consecutive same-name tool_use blocks into groups. */
function groupBlocks(blocks: ContentBlock[]): ContentBlock[] {
  const result: ContentBlock[] = [];
  let pending: ToolUseBlock[] = [];

  const flush = () => {
    if (pending.length === 0) return;
    if (pending.length >= 3) {
      // Merge: keep first, set groupLabel + groupedWith
      const first = { ...pending[0] };
      first.groupedWith = pending.slice(1).map((b) => b.id);
      first.groupLabel = `${first.name} ${pending.length} files`;
      result.push(first);
    } else {
      result.push(...pending);
    }
    pending = [];
  };

  for (const block of blocks) {
    if (block.type === "tool_use" && block.status === "success" && !block.retryOf) {
      const last = pending[pending.length - 1];
      if (last && last.name === block.name && last.status === "success") {
        pending.push(block);
      } else {
        flush();
        pending.push(block);
      }
    } else {
      flush();
      result.push(block);
    }
  }
  flush();
  return result;
}

export function BlocksMessage({ blocks, role, metadata }: BlocksMessageProps) {
  const isUser = role === "user";
  const grouped = isUser ? blocks : groupBlocks(blocks);

  return (
    <div className={`blocks-message${isUser ? " blocks-user" : ""}`}>
      {grouped.map((block, i) => {
        if (block.type === "text") {
          return (
            <div key={i} className="blocks-text">
              {isUser ? block.content : <MarkdownRenderer content={block.content} />}
            </div>
          );
        }
        if (block.type === "tool_use") {
          return <InlineToolBlock key={block.id || i} block={block} />;
        }
        if (block.type === "thought") {
          return <InlineThoughtBlock key={i} block={block} />;
        }
        return null;
      })}

      {!isUser && metadata && (
        <div className="blocks-meta">
          {metadata.steps > 0 && <span>{metadata.steps} steps</span>}
          {metadata.tokens > 0 && <span>· {(metadata.tokens / 1000).toFixed(1)}K tokens</span>}
          {metadata.durationMs > 0 && <span>· {(metadata.durationMs / 1000).toFixed(0)}s</span>}
        </div>
      )}
    </div>
  );
}
