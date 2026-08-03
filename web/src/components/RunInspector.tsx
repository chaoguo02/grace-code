import { useEffect, useMemo, useState } from "react";

import { getSessionDiffs } from "../api/diffs";
import { getRunEvidence, getTimeline } from "../api/sessions";
import { getSessionStats } from "../api/stats";
import { selectSessionUi, useChatStore } from "../stores/chatStore";
import { useSessionStore } from "../stores/sessionStore";
import type {
  TimelineResponse,
  TurnTimeline,
  WsMessage,
} from "../types";
import type {
  RunEvidenceRecord,
  RunVerification,
  RunWorkspaceDelta,
} from "../types/events";
import type { SessionDiff, SessionStats } from "../types/stats";
import type { NavigationTarget, ViewName } from "../navigation";
import { SessionRequiredState } from "./SessionRequiredState";

type InspectorState =
  | { phase: "idle" }
  | { phase: "loading" }
  | { phase: "ready" }
  | { phase: "error"; message: string };

interface ToolUsage {
  name: string;
  calls: number;
  successes: number;
  failures: number;
}

function formatDuration(start?: string, end?: string) {
  if (!start) return "—";
  const startMs = Date.parse(start);
  const endMs = end ? Date.parse(end) : Date.now();
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) return "—";
  const seconds = Math.max(0, Math.round((endMs - startMs) / 1000));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function shortId(value?: string) {
  return value ? value.slice(0, 8) : "—";
}

function titleCase(value?: string) {
  if (!value) return "Unknown";
  return value
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter: string) => letter.toUpperCase());
}

function eventCount(events: WsMessage[], type: WsMessage["type"]) {
  return events.filter((event) => event.type === type).length;
}

export function deriveToolUsage(events: WsMessage[]): ToolUsage[] {
  const observations = new Map<string, { success: number; failure: number }>();
  for (const event of events) {
    if (event.type !== "observation") continue;
    const name = event.tool_name || "Unknown tool";
    const current = observations.get(name) || { success: 0, failure: 0 };
    const failed = event.status === "error" || event.status === "failed" || !!event.error;
    if (failed) current.failure += 1;
    else current.success += 1;
    observations.set(name, current);
  }

  const calls = new Map<string, number>();
  for (const event of events) {
    if (event.type !== "tool_call") continue;
    calls.set(event.name, (calls.get(event.name) || 0) + 1);
  }

  return Array.from(calls.entries())
    .map(([name, count]) => {
      const outcomes = observations.get(name) || { success: 0, failure: 0 };
      return {
        name,
        calls: count,
        successes: outcomes.success,
        failures: outcomes.failure,
      };
    })
    .sort((a, b) => b.calls - a.calls || a.name.localeCompare(b.name));
}

function resolveRunStatus(turn: TurnTimeline) {
  if (turn.meta.status) return turn.meta.status;
  const terminal = turn.trace_events.find((event) => event.type === "run_terminal");
  return terminal?.type === "run_terminal" ? terminal.status : "unknown";
}

function resolveSummary(turn: TurnTimeline) {
  const terminal = turn.trace_events.find((event) => event.type === "run_terminal");
  if (terminal?.type === "run_terminal" && terminal.summary) return terminal.summary;
  return turn.assistant_message?.content || "";
}

function resolveVerification(turn: TurnTimeline): RunVerification {
  return turn.meta.verification || {
    status: "not_applicable",
    reason: "none",
    checks: [],
  };
}

function resolveWorkspaceDelta(turn: TurnTimeline): RunWorkspaceDelta {
  return turn.meta.workspace_delta || {};
}

function RunMetric({
  label,
  value,
  hint,
}: {
  label: string;
  value: string | number;
  hint: string;
}) {
  return (
    <div className="run-inspector-metric">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{hint}</small>
    </div>
  );
}

function EmptyRunInspector() {
  return (
    <SessionRequiredState
      mark="RI"
      title="Select a session to inspect its runs"
      description="Run Inspector connects execution, tools, verification, subagents, and workspace changes into one explainable view."
    />
  );
}

interface RunInspectorProps {
  requestedRunId?: string;
  onNavigate?: (view: ViewName, target?: NavigationTarget) => void;
}

export function RunInspector({
  requestedRunId,
  onNavigate,
}: RunInspectorProps = {}) {
  const activeId = useSessionStore((state) => state.activeId);
  const activeDetail = useSessionStore((state) => state.activeDetail);
  const liveEventKey = useChatStore((state) => {
    if (!activeId) return "";
    const events = selectSessionUi(state, activeId).events;
    const event = events.find(
      (candidate) => candidate.type === "run_terminal" || candidate.type === "run_started",
    );
    return event ? `${event.type}:${event.run_id || ""}:${event.sequence || 0}` : "";
  });
  const liveEvidenceCount = useChatStore((state) => {
    if (!activeId) return 0;
    return Object.keys(selectSessionUi(state, activeId).evidenceById).length;
  });

  const [state, setState] = useState<InspectorState>({ phase: "idle" });
  const [timeline, setTimeline] = useState<TimelineResponse | null>(null);
  const [stats, setStats] = useState<SessionStats | null>(null);
  const [diffs, setDiffs] = useState<SessionDiff[]>([]);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [evidence, setEvidence] = useState<RunEvidenceRecord[]>([]);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    if (!activeId) {
      setTimeline(null);
      setStats(null);
      setDiffs([]);
      setSelectedRunId("");
      setState({ phase: "idle" });
      return;
    }

    const controller = new AbortController();
    setState({ phase: "loading" });
    Promise.all([
      getTimeline(activeId, controller.signal, 0, 500),
      getSessionStats(activeId, controller.signal).catch(() => null),
      getSessionDiffs(activeId, undefined, controller.signal).catch(() => []),
    ])
      .then(([nextTimeline, nextStats, nextDiffs]) => {
        setTimeline(nextTimeline);
        setStats(nextStats);
        setDiffs(nextDiffs);
        setSelectedRunId((current) => {
          const available = nextTimeline.turns
            .filter((turn) => turn.run_id)
            .sort((a, b) => b.turn_index - a.turn_index);
          if (
            requestedRunId
            && available.some((turn) => turn.run_id === requestedRunId)
          ) {
            return requestedRunId;
          }
          if (current && available.some((turn) => turn.run_id === current)) return current;
          return nextTimeline.active_run?.run_id || available[0]?.run_id || "";
        });
        setState({ phase: "ready" });
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        setState({
          phase: "error",
          message: error instanceof Error ? error.message : "Failed to load run data",
        });
      });
    return () => controller.abort();
  }, [activeId, liveEventKey, reloadKey, requestedRunId]);

  const runs = useMemo(
    () => [...(timeline?.turns || [])]
      .filter((turn) => turn.run_id)
      .sort((a, b) => b.turn_index - a.turn_index),
    [timeline],
  );

  const selectedRun = useMemo(
    () => requestedRunId
      ? runs.find((run) => run.run_id === requestedRunId) || null
      : runs.find((run) => run.run_id === selectedRunId) || runs[0] || null,
    [requestedRunId, runs, selectedRunId],
  );

  useEffect(() => {
    if (!activeId || !selectedRun?.run_id) {
      setEvidence([]);
      return;
    }
    const controller = new AbortController();
    getRunEvidence(activeId, selectedRun.run_id, controller.signal)
      .then((result) => setEvidence(result.evidence))
      .catch(() => {
        if (!controller.signal.aborted) setEvidence([]);
      });
    return () => controller.abort();
  }, [activeId, selectedRun?.run_id, liveEventKey]);

  useEffect(() => {
    if (!activeId || !selectedRun?.run_id || liveEvidenceCount === 0) return;
    const live = Object.values(
      selectSessionUi(useChatStore.getState(), activeId).evidenceById,
    ).filter((entry) => entry.root_run_id === selectedRun.run_id);
    if (!live.length) return;
    setEvidence((persisted) => {
      const merged = new Map(
        persisted.map((entry) => [entry.evidence_id, entry]),
      );
      for (const entry of live) merged.set(entry.evidence_id, entry);
      return Array.from(merged.values()).sort(
        (left, right) => left.sequence - right.sequence,
      );
    });
  }, [activeId, selectedRun?.run_id, liveEvidenceCount]);

  const model = useMemo(() => {
    if (!selectedRun) return null;
    const events = selectedRun.trace_events;
    const tools = deriveToolUsage(events);
    const verification = resolveVerification(selectedRun);
    const workspace = resolveWorkspaceDelta(selectedRun);
    const subagentStarts = events.filter((event) => event.type === "subagent_start");
    const subagentStops = events.filter((event) => event.type === "subagent_stop");
    const approvals = eventCount(events, "approval_required");
    const observations = eventCount(events, "observation");
    const failedObservations = events.filter(
      (event) => event.type === "observation"
        && (event.status === "error" || event.status === "failed" || !!event.error),
    ).length;
    const terminal = events.find((event) => event.type === "run_terminal");
    const terminationReason = selectedRun.meta.termination_reason
      || (terminal?.type === "run_terminal" ? terminal.termination_reason : "")
      || "none";
    return {
      events,
      tools,
      verification,
      workspace,
      approvals,
      observations,
      failedObservations,
      subagentStarts,
      subagentStops,
      status: resolveRunStatus(selectedRun),
      summary: resolveSummary(selectedRun),
      terminationReason,
      duration: formatDuration(selectedRun.meta.started_at, selectedRun.meta.completed_at),
    };
  }, [selectedRun]);

  if (!activeId) return <EmptyRunInspector />;

  return (
    <section className="run-inspector" data-view-name="runs">
      <header className="run-inspector-header">
        <div>
          <div className="summary-label">Run Inspector</div>
          <h1>{activeDetail?.title || "Session run"}</h1>
          <p>
            Explainable execution facts for session <code>{shortId(activeId)}</code>
          </p>
        </div>
        <button
          type="button"
          className="run-inspector-refresh"
          onClick={() => setReloadKey((value) => value + 1)}
          disabled={state.phase === "loading"}
        >
          {state.phase === "loading" ? "Loading…" : "Refresh"}
        </button>
      </header>

      {state.phase === "error" && (
        <div className="run-inspector-error">
          <strong>Run data could not be loaded.</strong>
          <span>{state.message}</span>
          <button type="button" onClick={() => setReloadKey((value) => value + 1)}>
            Try again
          </button>
        </div>
      )}

      {state.phase === "loading" && !timeline && (
        <div className="run-inspector-skeleton">
          {Array.from({ length: 8 }, (_, index) => <span key={index} />)}
        </div>
      )}

      {state.phase === "ready" && runs.length === 0 && (
        <div className="run-inspector-empty compact">
          <div className="run-inspector-empty-mark">0</div>
          <h2>No submitted runs yet</h2>
          <p>Send a message in Chat. Its execution contract will appear here.</p>
        </div>
      )}

      {state.phase === "ready" && requestedRunId && runs.length > 0 && !selectedRun && (
        <div className="run-inspector-error">
          <strong>The requested run is not available in this session.</strong>
          <span><code>{requestedRunId}</code></span>
          <button type="button" onClick={() => onNavigate?.("runs")}>
            Show latest run
          </button>
        </div>
      )}

      {selectedRun && model && (
        <>
          <div className="run-inspector-run-strip" role="tablist" aria-label="Session runs">
            {runs.map((run) => {
              const status = resolveRunStatus(run);
              return (
                <button
                  type="button"
                  role="tab"
                  aria-selected={run.run_id === selectedRun.run_id}
                  className={run.run_id === selectedRun.run_id ? "active" : ""}
                  key={run.run_id}
                  onClick={() => {
                    setSelectedRunId(run.run_id);
                    onNavigate?.("runs", {
                      runId: run.run_id,
                      turnId: run.turn_id,
                    });
                  }}
                >
                  <span>Run {run.turn_index + 1}</span>
                  <small>{shortId(run.run_id)}</small>
                  <i className={`run-status-dot status-${status}`} />
                </button>
              );
            })}
          </div>

          <div className="run-inspector-outcome">
            <div className={`run-inspector-status status-${model.status}`}>
              <span className="run-inspector-status-mark" />
              {titleCase(model.status)}
            </div>
            <div className="run-inspector-outcome-copy">
              <span>Termination: {titleCase(model.terminationReason)}</span>
              <strong>{model.summary || "Run completed without a persisted summary."}</strong>
            </div>
            <div className="run-inspector-outcome-id">
              <span>Run ID</span>
              <code>{selectedRun.run_id}</code>
            </div>
            <nav className="run-evidence-links" aria-label="Run evidence">
              <button
                type="button"
                onClick={() => onNavigate?.("context", {
                  runId: selectedRun.run_id,
                  turnId: selectedRun.turn_id,
                })}
              >
                Context
              </button>
            </nav>
          </div>

          <div className="run-inspector-metrics">
            <RunMetric
              label="Steps"
              value={selectedRun.meta.steps}
              hint={`${model.events.length} persisted events`}
            />
            <RunMetric
              label="Tokens"
              value={selectedRun.meta.tokens.toLocaleString()}
              hint={`${stats?.total_tokens?.toLocaleString() || 0} in session`}
            />
            <RunMetric label="Duration" value={model.duration} hint="Wall-clock run time" />
            <RunMetric
              label="Tool calls"
              value={model.tools.reduce((sum, tool) => sum + tool.calls, 0)}
              hint={`${model.failedObservations} failed observations`}
            />
            <RunMetric
              label="Subagents"
              value={model.subagentStarts.length}
              hint={`${model.subagentStops.length} terminal`}
            />
            <RunMetric
              label="Approvals"
              value={model.approvals}
              hint="Human control points"
            />
            <RunMetric
              label="Evidence"
              value={evidence.length}
              hint={`${evidence.filter((item) => item.status !== "succeeded").length} non-success`}
            />
          </div>

          <div className="run-inspector-primary-grid">
            <article className="run-inspector-card execution">
              <div className="run-inspector-card-heading">
                <div>
                  <span>Evidence chain</span>
                  <h2>Persisted runtime facts</h2>
                </div>
                <span className="run-inspector-badge">{evidence.length} records</span>
              </div>
              <div className="run-stage-flow">
                {evidence.length === 0 && (
                  <div className="run-stage">
                    <strong>No evidence recorded</strong>
                    <span>This run did not produce evidence-chain records.</span>
                  </div>
                )}
                {evidence.slice(-12).map((entry) => (
                  <div
                    className={`run-stage ${entry.status === "succeeded" ? "complete" : ""}`}
                    key={entry.evidence_id}
                  >
                    <i>{entry.sequence}</i>
                    <strong>{titleCase(entry.kind)}</strong>
                    <span>
                      {entry.path || entry.tool_name || entry.summary || entry.evidence_id}
                    </span>
                  </div>
                ))}
              </div>
            </article>
            <article className="run-inspector-card execution">
              <div className="run-inspector-card-heading">
                <div>
                  <span>Execution flow</span>
                  <h2>From request to verified outcome</h2>
                </div>
                <span className="run-inspector-badge">{model.events.length} events</span>
              </div>
              <div className="run-stage-flow">
                <div className="run-stage complete">
                  <i>1</i><strong>Input</strong>
                  <span>{selectedRun.user_message ? "Prompt captured" : "Runtime initiated"}</span>
                </div>
                <div className="run-stage complete">
                  <i>2</i><strong>Reason</strong>
                  <span>{eventCount(model.events, "thought")} thought blocks</span>
                </div>
                <div className={model.tools.length ? "run-stage complete" : "run-stage muted"}>
                  <i>3</i><strong>Act</strong>
                  <span>{model.tools.length} capabilities used</span>
                </div>
                <div className={model.observations ? "run-stage complete" : "run-stage muted"}>
                  <i>4</i><strong>Observe</strong>
                  <span>{model.observations} results collected</span>
                </div>
                <div className={`run-stage verification-${model.verification.status}`}>
                  <i>5</i><strong>Verify</strong>
                  <span>{titleCase(model.verification.status)}</span>
                </div>
              </div>

              <div className="run-agent-lane">
                <div className="run-agent-node primary">
                  <span>Main agent</span>
                  <strong>{activeDetail?.agent_name || "build"}</strong>
                  <small>{activeDetail?.mode || "primary"} context</small>
                </div>
                <div className="run-agent-connector">
                  <span>{model.subagentStarts.length ? "delegated" : "owned locally"}</span>
                </div>
                {model.subagentStarts.length ? (
                  <div className="run-agent-children">
                    {model.subagentStarts.map((event, index) => (
                      <div className="run-agent-node" key={`${event.child_session_id}-${index}`}>
                        <span>Subagent</span>
                        <strong>{event.agent_name || "worker"}</strong>
                        <small>{shortId(event.child_session_id)}</small>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="run-agent-node quiet">
                    <span>Delegation</span>
                    <strong>Not used</strong>
                    <small>Single-agent execution</small>
                  </div>
                )}
              </div>
            </article>

            <article className="run-inspector-card verification">
              <div className="run-inspector-card-heading">
                <div>
                  <span>Trust boundary</span>
                  <h2>Verification</h2>
                </div>
                <span className={`run-verification-pill status-${model.verification.status}`}>
                  {titleCase(model.verification.status)}
                </span>
              </div>
              <dl className="run-inspector-facts">
                <div><dt>Reason</dt><dd>{titleCase(model.verification.reason)}</dd></div>
                <div><dt>Checks</dt><dd>{model.verification.checks?.length || 0}</dd></div>
                <div><dt>Run-scoped delta</dt><dd>{model.workspace.is_run_scoped ? "Yes" : "No"}</dd></div>
                <div><dt>Evidence source</dt><dd>{titleCase(model.workspace.source)}</dd></div>
              </dl>
              <div className="run-check-list">
                {(model.verification.checks || []).map((check, index) => (
                  <div className={`run-check status-${check.status}`} key={`${check.name}-${index}`}>
                    <i />
                    <div>
                      <strong>{check.name}</strong>
                      <span>{check.command || check.detail || titleCase(check.status)}</span>
                    </div>
                    <small>{check.duration_ms ? `${check.duration_ms}ms` : titleCase(check.status)}</small>
                  </div>
                ))}
                {!model.verification.checks?.length && (
                  <div className="run-inspector-note">
                    No individual verification checks were persisted for this run.
                  </div>
                )}
              </div>
            </article>
          </div>

          <div className="run-inspector-secondary-grid">
            <article className="run-inspector-card">
              <div className="run-inspector-card-heading">
                <div>
                  <span>Capability usage</span>
                  <h2>Tools, MCP, and Skills</h2>
                </div>
              </div>
              <div className="run-tool-table">
                <div className="run-tool-row header">
                  <span>Capability</span><span>Source</span><span>Calls</span><span>Outcome</span>
                </div>
                {model.tools.map((tool) => (
                  <div className="run-tool-row" key={tool.name}>
                    <strong>{tool.name}</strong>
                    <span>{tool.name.startsWith("mcp__") ? "MCP" : tool.name === "Skill" ? "Skill" : "Built-in"}</span>
                    <span>{tool.calls}</span>
                    <span className={tool.failures ? "bad" : "good"}>
                      {tool.failures ? `${tool.failures} failed` : `${tool.successes || tool.calls} ok`}
                    </span>
                  </div>
                ))}
                {!model.tools.length && (
                  <div className="run-inspector-note">This run completed without tool calls.</div>
                )}
              </div>
            </article>

            <article className="run-inspector-card">
              <div className="run-inspector-card-heading">
                <div>
                  <span>Workspace impact</span>
                  <h2>Files and review state</h2>
                </div>
                <span className="run-inspector-badge">
                  {model.workspace.changed_files?.length || 0} changed
                </span>
              </div>
              <div className="run-file-list">
                {(model.workspace.changed_files || []).map((file) => {
                  const review = diffs.find((diff) => diff.file_path === file);
                  return (
                    <div className="run-file-row" key={file}>
                      <code>{file}</code>
                      <span className={`status-${review?.status || "recorded"}`}>
                        {review?.status || "recorded"}
                      </span>
                    </div>
                  );
                })}
                {!model.workspace.changed_files?.length && (
                  <div className="run-inspector-note">
                    {model.workspace.has_changes
                      ? "The run reports changes, but no file list was persisted."
                      : "No workspace changes attributed to this run."}
                  </div>
                )}
              </div>
              <div className="run-workspace-footer">
                <span>Patch {model.workspace.patch_available ? "available" : "not available"}</span>
                <span>{diffs.filter((diff) => diff.status === "pending").length} pending reviews</span>
              </div>
            </article>
          </div>
        </>
      )}
    </section>
  );
}
