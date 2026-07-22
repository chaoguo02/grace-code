import { useMemo, useState } from "react";
import type { WsMessage } from "../types";
import { DiffBlock } from "./DiffBlock";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { formatValue } from "../utils/format";

function iconFor(event: WsMessage): string {
  switch (event.type) {
    case "thought":
      return "T";
    case "tool_call":
      return "A";
    case "observation":
      return "O";
    case "reflection":
      return "R";
    case "approval_required":
    case "approval_timeout":
      return "!";
    case "subagent_start":
      return "S";
    case "subagent_stop":
      return "D";
    case "plan_ready":
      return "P";
    case "worktree_resolved":
      return "W";
    case "status":
      return "F";
    default:
      return "E";
  }
}

function labelFor(event: WsMessage): string {
  switch (event.type) {
    case "thought":
      return "Thinking";
    case "tool_call":
      return "Action";
    case "observation":
      return "Observation";
    case "reflection":
      return "Reflection";
    case "approval_required":
    case "approval_timeout":
      return "Approval";
    case "subagent_start":
    case "subagent_stop":
      return "Subagent";
    case "plan_ready":
      return "Plan";
    case "worktree_resolved":
      return "Worktree";
    case "status":
      return "Final";
    default:
      return "Event";
  }
}

function titleFor(event: WsMessage): string {
  switch (event.type) {
    case "thought":
      return "Reasoning about the next step";
    case "tool_call":
      return event.name || "Tool call";
    case "observation":
      return event.tool_name || "Tool result";
    case "reflection":
      return "Reassessing the run";
    case "approval_required":
      return `Approval required for ${event.tool_name || "tool"}`;
    case "approval_timeout":
      return "Approval request timed out";
    case "subagent_start":
      return `Spawned ${event.agent_name || "subagent"}`.trim();
    case "subagent_stop":
      return "Subagent completed";
    case "plan_ready":
      return "Execution plan is ready";
    case "worktree_resolved":
      return `Worktree ${event.action || "review"} ${event.status || ""}`.trim();
    case "status":
      return event.status === "finish" ? "Final response" : event.status || "Status";
    default:
      return "Event";
  }
}

function formatDuration(ms?: number) {
  if (!ms || ms <= 0) return null;
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`;
}

function formatTokens(value?: number) {
  if (!value || value <= 0) return null;
  if (value >= 1000) return `~${(value / 1000).toFixed(value >= 10_000 ? 0 : 1)}K tok`;
  return `~${value} tok`;
}

function summarizeToolTarget(event: WsMessage): string {
  const params = (event as { params?: Record<string, unknown> }).params || {};
  const keys = ["path", "file_path", "target_file", "command", "pattern", "url"];
  for (const key of keys) {
    const value = params[key];
    if (value != null) return String(value);
  }
  const firstEntry = Object.entries(params)[0];
  if (!firstEntry) return "No explicit target";
  return `${firstEntry[0]}: ${String(firstEntry[1])}`;
}

function summaryFor(event: WsMessage): string {
  switch (event.type) {
    case "thought":
    case "reflection":
      return (event.content || "").slice(0, 140) || "No summary";
    case "tool_call":
      return summarizeToolTarget(event);
    case "observation":
      return (event.output || event.error || "").replace(/\s+/g, " ").slice(0, 160) || "No observation output";
    case "approval_required":
      return (event.decision_reason || event.thought || "This action needs review before execution.").slice(0, 160);
    case "approval_timeout":
      return `Request ${event.request_id?.slice(0, 8) || "???"} was not resolved in time.`;
    case "subagent_start":
      return `Child session ${event.child_session_id?.slice(0, 8) || "???"} is now running.`;
    case "subagent_stop":
      return `${event.child_session_id?.slice(0, 8) || "???"} finished with ${event.status || "completed"}.`;
    case "plan_ready":
      return (event.plan_text || event.result?.summary || "The agent paused for plan review.").slice(0, 180);
    case "worktree_resolved":
      return (event.message || `${event.action} → ${event.status}`).slice(0, 180);
    case "status":
      return (event.message || event.result?.summary || event.error || "").slice(0, 180) || "No final content";
    default:
      return ((event as { message?: string; content?: string }).message || (event as { content?: string }).content || "").slice(0, 140) || "No summary";
  }
}

function detailFor(event: WsMessage): string {
  if (event.type === "thought" || event.type === "reflection") return event.content || "";
  if (event.type === "tool_call") return formatValue(event.params || {});
  if (event.type === "observation") return event.output || event.error || "";
  if (event.type === "approval_required") return formatValue(event.params || {});
  if (event.type === "approval_timeout") return `Timed out request: ${event.request_id}`;
  if (event.type === "subagent_start") return `Child session ${event.child_session_id || ""}`.trim();
  if (event.type === "subagent_stop") return `${event.child_session_id} · ${event.status || ""}`.trim();
  if (event.type === "plan_ready") return event.plan_text || event.result?.summary || "";
  if (event.type === "worktree_resolved") return event.message || `${event.action}: ${event.status}`;
  if (event.type === "status") return event.message || event.result?.summary || event.error || "";
  return JSON.stringify(event, null, 2);
}

function cardClass(event: WsMessage): string {
  switch (event.type) {
    case "thought":
      return "trace-card trace-card-thinking";
    case "tool_call":
      return "trace-card trace-card-action";
    case "observation":
      return `trace-card ${event.status === "error" ? "trace-card-observation-error" : "trace-card-observation"}`;
    case "reflection":
      return "trace-card trace-card-reflection";
    case "approval_required":
      return "trace-card trace-card-approval";
    case "approval_timeout":
      return "trace-card trace-card-observation-error";
    case "subagent_start":
      return "trace-card trace-card-subagent";
    case "subagent_stop":
      return "trace-card trace-card-subagent-stop";
    case "plan_ready":
      return "trace-card trace-card-plan";
    case "worktree_resolved":
      return event.status === "applied"
        ? "trace-card trace-card-worktree-success"
        : event.status === "discarded"
          ? "trace-card trace-card-worktree-discarded"
          : "trace-card trace-card-worktree";
    case "status":
      return "trace-card trace-card-final";
    default:
      return "trace-card";
  }
}

function supportsExpansion(event: WsMessage): boolean {
  return [
    "thought",
    "tool_call",
    "observation",
    "reflection",
    "approval_required",
    "plan_ready",
    "status",
    "worktree_resolved",
    "subagent_start",
    "subagent_stop",
  ].includes(event.type);
}

export function WsEventBlock({ event }: { event: WsMessage }) {
  const [expanded, setExpanded] = useState(
    event.type === "approval_required" || event.type === "plan_ready" || event.type === "worktree_resolved",
  );

  const ev = event as {
    duration_ms?: number;
    token_estimate?: number;
    child_session_id?: string;
    step?: number;
    status?: string;
    diff?: string;
  };
  const duration = formatDuration(
    event.type === "tool_call" || event.type === "observation" || event.type === "status"
      ? ev.duration_ms
      : undefined,
  );
  const tokens = formatTokens(ev.token_estimate);
  const summary = useMemo(() => summaryFor(event), [event]);
  const detail = useMemo(() => detailFor(event), [event]);
  const expandable = supportsExpansion(event);
  const isChildEvent = !!ev.child_session_id;

  const isSkippableStatus = event.type === "status" && !["finish", "gave_up", "completed", "failed"].includes(event.status || "");

  if (isSkippableStatus) {
    return null;
  }

  return (
    <div
      className={`trace-block trace-block-${event.type}`}
      style={isChildEvent ? { marginLeft: 20, borderLeft: "2px solid var(--accent-soft)", paddingLeft: 10 } : undefined}
    >
      {isChildEvent && (
        <div style={{ fontSize: 9, color: "var(--accent)", marginBottom: 2, textTransform: "uppercase", letterSpacing: "0.5px" }}>
          subagent lane {(ev as { child_session_id?: string }).child_session_id?.slice(0, 8) || "???"}
        </div>
      )}
      <div className="trace-rail-node" />
      <div className={cardClass(event)}>
        <div className="trace-header">
          <div className="trace-header-main">
            <div className="trace-icon">{iconFor(event)}</div>
            <div className="trace-head-copy">
              <div className="trace-label">{labelFor(event)}</div>
              <div className="trace-title">{titleFor(event)}</div>
              <div className="trace-summary">{summary}</div>
            </div>
          </div>
          <div className="trace-meta">
            {ev.step != null && <span className="trace-pill">Step {ev.step}</span>}
            {duration && <span className="trace-pill trace-pill-metric">⚡ {duration}</span>}
            {tokens && <span className="trace-pill trace-pill-metric">{tokens}</span>}
            {ev.status && event.type !== "status" && <span className="trace-pill">{ev.status}</span>}
            {expandable && (
              <button
                type="button"
                className="trace-expand-btn"
                onClick={() => setExpanded((v) => !v)}
              >
                {expanded ? "Hide" : "Expand"}
              </button>
            )}
          </div>
        </div>

        {expanded && (
          <div className={`trace-detail trace-detail-${event.type}`}>
            {(event.type === "tool_call" || event.type === "approval_required") && <pre>{detail}</pre>}

            {(event.type === "thought"
              || event.type === "reflection"
              || event.type === "status"
              || event.type === "plan_ready"
              || event.type === "worktree_resolved"
              || event.type === "subagent_start"
              || event.type === "subagent_stop"
              || event.type === "approval_timeout") && <MarkdownRenderer className="trace-body-copy" content={detail} />}

            {event.type === "observation" && !ev.diff && <MarkdownRenderer content={detail} />}

            {event.type === "plan_ready" && (
              <div className="trace-inline-note">
                Review the plan below or in the Plan tab before continuing execution.
              </div>
            )}

            {ev.diff && event.type === "observation" && (
              <div className="trace-diff-panel">
                <div className="trace-diff-header">Diff review</div>
                <DiffBlock diff={ev.diff} compact />
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
