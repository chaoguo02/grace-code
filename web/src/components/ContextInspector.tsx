import { useEffect, useMemo, useState } from "react";
import { getSessionContext } from "../api/stats";
import { compactSession } from "../api/sessions";
import { useSessionStore } from "../stores/sessionStore";
import type {
  ContextSnapshot,
  ContextSnapshotStats,
  SessionContextInspection,
} from "../types/stats";
import { ViewStatePanel } from "./ViewStatePanel";

interface ContextSegment {
  key: string;
  label: string;
  tokens: number;
  percent: number;
}

export interface ContextComposition {
  segments: ContextSegment[];
  utilization: number;
  pressure: "low" | "moderate" | "high" | "critical" | "over";
  unclassifiedTokens: number;
}

const EMPTY_INSPECTION: SessionContextInspection = {
  session_id: "",
  snapshots: [],
  memory_recalls: [],
  actual_usage: {
    tool_names: [],
    mcp_tools: [],
    skill_tool_used: false,
  },
  disclosure: {
    prompt_content_included: false,
    token_counts_are_estimates: true,
    snapshot_source: "provider_request_assembly",
  },
};

export function deriveContextComposition(
  stats: ContextSnapshotStats,
): ContextComposition {
  const budget = Math.max(1, stats.request_budget_tokens || 0);
  const known = [
    ["system", "System + project", stats.system_tokens || 0],
    ["memory", "Memory", stats.memory_tokens || 0],
    ["session", "Session", stats.session_tokens || 0],
    ["task", "Task history", stats.task_tokens || 0],
  ] as const;
  const knownTotal = known.reduce((total, item) => total + item[2], 0);
  const unclassifiedTokens = Math.max(
    0,
    (stats.estimated_total_tokens || 0) - knownTotal,
  );
  const rawSegments: Array<readonly [string, string, number]> = [
    ...known,
    ["other", "Message overhead", unclassifiedTokens],
  ];
  const utilization = stats.estimated_total_tokens / budget;
  return {
    segments: rawSegments
      .filter((item) => item[2] > 0)
      .map(([key, label, tokens]) => ({
        key,
        label,
        tokens,
        percent: Math.min(100, (tokens / budget) * 100),
      })),
    utilization,
    pressure: utilization > 1
      ? "over"
      : utilization >= 0.95
        ? "critical"
        : utilization >= 0.85
          ? "high"
          : utilization >= 0.70
            ? "moderate"
          : "low",
    unclassifiedTokens,
  };
}

function formatTokens(tokens?: number) {
  const value = tokens || 0;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return value.toLocaleString();
}

function formatTimestamp(value?: string) {
  if (!value) return "Unknown time";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function EmptyContext({ selected }: { selected: boolean }) {
  return (
    <div className="context-empty">
      <div className="context-empty-mark">CTX</div>
      <h3>{selected ? "No persisted context snapshots" : "Select a session"}</h3>
      <p>
        {selected
          ? "Context telemetry is recorded for new provider requests. Run or continue this session to populate the inspector."
          : "Choose a session to inspect what was assembled for each model request."}
      </p>
    </div>
  );
}

interface ContextInspectorProps {
  requestedRunId?: string;
}

export function ContextInspector({
  requestedRunId,
}: ContextInspectorProps = {}) {
  const activeId = useSessionStore((state) => state.activeId);
  const activeDetail = useSessionStore((state) => state.activeDetail);
  const [inspection, setInspection] = useState(EMPTY_INSPECTION);
  const [selectedSnapshotId, setSelectedSnapshotId] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [showAll, setShowAll] = useState(false);
  const [compacting, setCompacting] = useState(false);
  const [compactNotice, setCompactNotice] = useState("");

  useEffect(() => setShowAll(false), [requestedRunId]);

  useEffect(() => {
    if (!activeId) {
      setInspection(EMPTY_INSPECTION);
      setSelectedSnapshotId(null);
      return;
    }
    const controller = new AbortController();
    let disposed = false;

    const load = (showLoading = false) => {
      if (showLoading) setLoading(true);
      getSessionContext(activeId, controller.signal)
        .then((next) => {
          if (disposed) return;
          setInspection(next);
          setError("");
          setSelectedSnapshotId((current) => {
            const candidates = requestedRunId && !showAll
              ? next.snapshots.filter((item) => item.run_id === requestedRunId)
              : next.snapshots;
            if (current && candidates.some((item) => item.id === current)) {
              return current;
            }
            return candidates[candidates.length - 1]?.id ?? null;
          });
        })
        .catch((reason) => {
          if (disposed || controller.signal.aborted) return;
          setError(reason instanceof Error ? reason.message : "Unable to load context telemetry");
        })
        .finally(() => {
          if (!disposed) setLoading(false);
        });
    };

    load(true);
    const timer = activeDetail?.status === "running"
      ? window.setInterval(() => load(false), 2000)
      : undefined;
    return () => {
      disposed = true;
      controller.abort();
      if (timer) window.clearInterval(timer);
    };
  }, [activeId, activeDetail?.status, requestedRunId, showAll]);

  const visibleSnapshots = requestedRunId && !showAll
    ? inspection.snapshots.filter((snapshot) => snapshot.run_id === requestedRunId)
    : inspection.snapshots;
  const selected = visibleSnapshots.find(
    (snapshot) => snapshot.id === selectedSnapshotId,
  ) || visibleSnapshots[visibleSnapshots.length - 1];
  const composition = useMemo(
    () => selected ? deriveContextComposition(selected.stats) : null,
    [selected],
  );
  const latestRecalls = useMemo(
    () => [...inspection.memory_recalls].reverse().slice(0, 12),
    [inspection.memory_recalls],
  );
  const injectedRecalls = inspection.memory_recalls.filter((item) => item.injected);
  const latest = visibleSnapshots[visibleSnapshots.length - 1];
  const compactCount = visibleSnapshots.filter(
    (snapshot) => snapshot.stats.compact_triggered,
  ).length;
  const pressureMessage = composition && (
    composition.utilization >= 0.95
      ? "需要立即压缩"
      : composition.utilization >= 0.85
        ? "建议压缩"
        : composition.utilization >= 0.70
          ? "可考虑压缩"
          : ""
  );
  const estimatedRelease = selected && composition
    ? Math.max(0, selected.stats.estimated_total_tokens
      - Math.round(selected.stats.request_budget_tokens * 0.55))
    : 0;
  const usedTools = new Set(inspection.actual_usage.tool_names);

  return (
    <section className="view active" data-view-name="context">
      <div className="context-page">
        <header className="context-hero">
          <div>
            <span className="summary-label">Context Inspector</span>
            <h2>What the model actually received</h2>
            <p>
              Request-level token composition, context reduction, memory recall,
              and available capabilities from persisted runtime facts.
            </p>
          </div>
          <div className="context-hero-metrics">
            <div>
              <span>Requests</span>
              <strong>{visibleSnapshots.length}</strong>
            </div>
            <div>
              <span>Latest load</span>
              <strong>
                {latest
                  ? `${Math.round(
                    (latest.stats.estimated_total_tokens
                      / Math.max(1, latest.stats.request_budget_tokens)) * 100,
                  )}%`
                  : "—"}
              </strong>
            </div>
            <div>
              <span>Compactions</span>
              <strong>{compactCount}</strong>
            </div>
            <div>
              <span>Memories injected</span>
              <strong>{injectedRecalls.length}</strong>
            </div>
          </div>
        </header>

        {error && <div className="context-error">{error}</div>}
        {requestedRunId && (
          <div className="evidence-target-banner">
            <span>
              {showAll ? "Showing all session requests" : "Filtered to run"}
              {!showAll && <code>{requestedRunId}</code>}
            </span>
            <button type="button" onClick={() => setShowAll((value) => !value)}>
              {showAll ? "Return to run" : "Show full session"}
            </button>
          </div>
        )}
        {loading && inspection.snapshots.length === 0 ? (
          <ViewStatePanel
            tone="loading"
            title="Loading context telemetry"
            description="Reading request-level token and capability snapshots."
          />
        ) : visibleSnapshots.length === 0 ? (
          requestedRunId ? (
            <div className="context-empty">
              <div className="context-empty-mark">CTX</div>
              <h3>No run-scoped context evidence</h3>
              <p>
                This run predates context identity persistence, or did not send
                a provider request. Session snapshots are not inferred as a match.
              </p>
              <button type="button" onClick={() => setShowAll(true)}>
                Show session-level snapshots
              </button>
            </div>
          ) : (
          <EmptyContext selected={Boolean(activeId)} />
          )
        ) : selected && composition ? (
          <>
            <section className="context-request-strip">
              <div className="context-section-heading">
                <div>
                  <span className="summary-label">Provider requests</span>
                  <h3>Select a request snapshot</h3>
                </div>
                <span>{activeDetail?.title || activeId}</span>
              </div>
              <div className="context-request-list">
                {visibleSnapshots.map((snapshot, index) => (
                  <button
                    key={snapshot.id}
                    type="button"
                    className={snapshot.id === selected.id ? "selected" : ""}
                    onClick={() => setSelectedSnapshotId(snapshot.id)}
                  >
                    <span>R{index + 1}</span>
                    <strong>Step {snapshot.step_number}</strong>
                    <small>{snapshot.request_kind}</small>
                    <small>
                      {Math.round(
                        snapshot.stats.estimated_total_tokens
                        / Math.max(1, snapshot.stats.request_budget_tokens) * 100,
                      )}% · {formatTimestamp(snapshot.created_at)}
                    </small>
                    {snapshot.stats.compact_triggered && <i>Compacted</i>}
                  </button>
                ))}
              </div>
            </section>

            <div className="context-main-grid">
              <section className="context-card context-budget-card">
                <div className="context-section-heading">
                  <div>
                    <span className="summary-label">Token budget</span>
                    <h3>
                      {formatTokens(selected.stats.estimated_total_tokens)}
                      {" / "}
                      {formatTokens(selected.stats.request_budget_tokens)}
                    </h3>
                  </div>
                  <span className={`context-pressure context-pressure-${composition.pressure}`}>
                    {composition.pressure} pressure
                  </span>
                </div>

                <div
                  className="context-budget-track"
                  aria-label={`${Math.round(composition.utilization * 100)} percent of context budget used`}
                >
                  {composition.segments.map((segment) => (
                    <span
                      key={segment.key}
                      className={`context-budget-segment context-budget-${segment.key}`}
                      style={{ width: `${segment.percent}%` }}
                      title={`${segment.label}: ${segment.tokens.toLocaleString()} tokens`}
                    />
                  ))}
                </div>
                <div className="context-budget-scale">
                  <span>0</span>
                  <span>{Math.round(composition.utilization * 100)}% used</span>
                  <span>{formatTokens(selected.stats.request_budget_tokens)}</span>
                </div>
                {pressureMessage && activeId && (
                  <div className="context-decision-state">
                    <span className={composition.utilization >= 0.95 ? "warning" : "good"}>C</span>
                    <p>
                      {pressureMessage}。预计可释放约 {formatTokens(estimatedRelease)} tokens。
                      {compactNotice && ` ${compactNotice}`}
                    </p>
                    <button
                      type="button"
                      disabled={compacting}
                      onClick={async () => {
                        setCompacting(true);
                        setCompactNotice("");
                        try {
                          await compactSession(activeId);
                          setCompactNotice("压缩任务已接受，完成后会记录实际释放量。");
                        } catch (reason) {
                          setCompactNotice(reason instanceof Error ? reason.message : "压缩请求失败");
                        } finally {
                          setCompacting(false);
                        }
                      }}
                    >
                      {compacting ? "正在提交…" : "立即压缩"}
                    </button>
                  </div>
                )}

                <div className="context-token-breakdown">
                  {composition.segments.map((segment) => (
                    <div key={segment.key}>
                      <span className={`context-token-dot context-budget-${segment.key}`} />
                      <span>{segment.label}</span>
                      <strong>{formatTokens(segment.tokens)}</strong>
                    </div>
                  ))}
                </div>

                <div className="context-secondary-facts">
                  <div>
                    <span>Repo map subset</span>
                    <strong>{formatTokens(selected.stats.repo_map_tokens)}</strong>
                  </div>
                  <div>
                    <span>Omitted before request</span>
                    <strong>{formatTokens(selected.stats.omitted_tokens)}</strong>
                  </div>
                  <div>
                    <span>Artifacts offloaded</span>
                    <strong>{formatTokens(selected.stats.artifact_summary_tokens)}</strong>
                  </div>
                </div>
              </section>

              <section className="context-card">
                <div className="context-section-heading">
                  <div>
                    <span className="summary-label">Reduction decision</span>
                    <h3>
                      {selected.stats.compact_triggered
                        ? "Conversation compacted"
                        : selected.stats.omitted_tokens > 0
                          ? "History trimmed"
                          : "No reduction required"}
                    </h3>
                  </div>
                </div>
                <div className="context-decision-state">
                  <span className={selected.stats.compact_triggered ? "warning" : "good"}>
                    {selected.stats.compact_triggered ? "C" : "✓"}
                  </span>
                  <p>
                    {selected.stats.compact_triggered
                      ? selected.stats.compact_reason || "Context planner requested compaction."
                      : selected.stats.omitted_tokens > 0
                        ? `${formatTokens(selected.stats.omitted_tokens)} tokens were excluded by the history budget.`
                        : "The request fit its context budget without compaction or history omission."}
                  </p>
                </div>
                <dl className="context-decision-facts">
                  <div><dt>Method</dt><dd>{selected.stats.compact_method || "None"}</dd></div>
                  <div><dt>Truncated</dt><dd>{selected.stats.compact_truncated ? "Yes" : "No"}</dd></div>
                  <div>
                    <dt>Source range</dt>
                    <dd>
                      {selected.stats.compact_source_range
                        ? selected.stats.compact_source_range.join("–")
                        : "Not applicable"}
                    </dd>
                  </div>
                  <div><dt>Request kind</dt><dd>{selected.request_kind}</dd></div>
                </dl>
              </section>
            </div>

            <div className="context-main-grid context-capability-grid">
              <section className="context-card">
                <div className="context-section-heading">
                  <div>
                    <span className="summary-label">Capability exposure</span>
                    <h3>
                      {selected.capabilities.tool_count || selected.capabilities.tool_names.length}
                      {" tools available"}
                    </h3>
                  </div>
                  <span>{inspection.actual_usage.tool_names.length} used</span>
                </div>
                <div className="context-chip-list">
                  {selected.capabilities.tool_names.map((name) => (
                    <span
                      key={name}
                      className={usedTools.has(name) ? "used" : ""}
                      title={usedTools.has(name) ? "Used in this session" : "Available to this request"}
                    >
                      {name}
                    </span>
                  ))}
                  {selected.capabilities.tool_names.length === 0 && (
                    <p className="context-muted">No tool schemas were exposed.</p>
                  )}
                </div>
              </section>

              <section className="context-card context-injection-card">
                <div>
                  <span className="summary-label">Runtime skill buffer</span>
                  <h3>{selected.capabilities.active_skills.length} active</h3>
                  <div className="context-chip-list">
                    {selected.capabilities.active_skills.map((name) => (
                      <span key={name} className="skill">{name}</span>
                    ))}
                    {selected.capabilities.active_skills.length === 0 && (
                      <p className="context-muted">No active skill retained by the runtime.</p>
                    )}
                  </div>
                </div>
                <div>
                  <span className="summary-label">MCP</span>
                  <h3>{selected.capabilities.mcp_servers.length} servers exposed</h3>
                  <div className="context-chip-list">
                    {selected.capabilities.mcp_servers.map((name) => (
                      <span key={name} className="mcp">{name}</span>
                    ))}
                    {selected.capabilities.mcp_servers.length === 0 && (
                      <p className="context-muted">No MCP tool schema in this request.</p>
                    )}
                  </div>
                </div>
              </section>
            </div>

            <section className="context-card">
              <div className="context-section-heading">
                <div>
                  <span className="summary-label">Memory recall</span>
                  <h3>Injected and omitted candidates</h3>
                </div>
                <span>{inspection.memory_recalls.length} decisions</span>
              </div>
              {latestRecalls.length === 0 ? (
                <p className="context-muted">No persisted memory recall decisions for this session.</p>
              ) : (
                <div className="context-memory-list">
                  {latestRecalls.map((recall, index) => (
                    <article key={`${recall.created_at}-${recall.memory_name}-${index}`}>
                      <span className={recall.injected ? "injected" : "omitted"}>
                        {recall.injected ? "Injected" : recall.omitted_reason || "Omitted"}
                      </span>
                      <div>
                        <strong>{recall.memory_name}</strong>
                        <p>{recall.reason || recall.description || "No selection reason recorded."}</p>
                      </div>
                      <dl>
                        <div><dt>Source</dt><dd>{recall.source}</dd></div>
                        <div><dt>Score</dt><dd>{Math.round((recall.score || 0) * 100)}%</dd></div>
                        <div><dt>Scope</dt><dd>{recall.scope || "—"}</dd></div>
                      </dl>
                    </article>
                  ))}
                </div>
              )}
            </section>

            <footer className="context-disclosure">
              <strong>Telemetry boundary</strong>
              <span>
                Prompt contents are not stored here. Token counts are estimates;
                snapshots describe the request at provider-assembly time.
              </span>
            </footer>
          </>
        ) : null}
      </div>
    </section>
  );
}
