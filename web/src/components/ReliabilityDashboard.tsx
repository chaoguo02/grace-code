import { useEffect, useMemo, useState } from "react";
import { getReliabilityOverview } from "../api/reliability";
import type {
  ReliabilityObjective,
  ReliabilityOverview,
  ReliabilityTrendPoint,
} from "../types/reliability";
import { ViewStatePanel } from "./ViewStatePanel";

export function deriveReliabilityBars(points: ReliabilityTrendPoint[]) {
  const maxRuns = Math.max(1, ...points.map((point) => point.runs));
  const maxTokens = Math.max(1, ...points.map((point) => point.tokens));
  return points.map((point) => ({
    ...point,
    run_height: Math.max(2, (point.runs / maxRuns) * 100),
    token_height: Math.max(2, (point.tokens / maxTokens) * 100),
  }));
}

function formatPercent(value: number | null) {
  return value == null ? "—" : `${(value * 100).toFixed(1)}%`;
}

function formatTokens(value: number | null) {
  if (value == null) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(1)}k` : String(Math.round(value));
}

function formatDuration(value: number | null) {
  if (value == null) return "—";
  if (value < 1000) return `${Math.round(value)}ms`;
  const seconds = value / 1000;
  return seconds < 60 ? `${seconds.toFixed(1)}s` : `${(seconds / 60).toFixed(1)}m`;
}

function formatObjectiveValue(objective: ReliabilityObjective, value: number | null) {
  if (value == null) return "no data";
  if (objective.id.includes("latency")) return formatDuration(value);
  return formatPercent(value);
}

interface ReliabilityDashboardProps {
  onOpenEvaluations?: () => void;
}

export function ReliabilityDashboard({
  onOpenEvaluations,
}: ReliabilityDashboardProps = {}) {
  const [days, setDays] = useState(30);
  const [overview, setOverview] = useState<ReliabilityOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    getReliabilityOverview(days, controller.signal)
      .then(setOverview)
      .catch((reason) => {
        if (reason?.name !== "AbortError") {
          setError(reason instanceof Error ? reason.message : String(reason));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [days]);

  const trend = useMemo(
    () => deriveReliabilityBars(overview?.trend || []),
    [overview],
  );

  if (loading && !overview) {
    return (
      <ViewStatePanel
        tone="loading"
        title="Aggregating persisted run health"
        description={`Computing project-level reliability over the last ${days} days.`}
      />
    );
  }
  if (error && !overview) {
    return (
      <ViewStatePanel
        tone="error"
        title="Reliability evidence could not be loaded"
        description={error}
      />
    );
  }
  if (!overview) return null;

  const summary = overview.summary;
  return (
    <div className="reliability-dashboard">
      <header className="reliability-hero">
        <div>
          <span className="reliability-eyebrow">Cross-session operations</span>
          <h1>Reliability & Resource Dashboard</h1>
          <p>
            Run outcomes, latency, tool reliability, and token consumption from
            persisted evidence. Currency cost stays unavailable until pricing is versioned.
          </p>
        </div>
        <div className="quality-header-actions">
          {onOpenEvaluations && (
            <button type="button" onClick={onOpenEvaluations}>
              Open evaluations
            </button>
          )}
          <div className="reliability-window">
            {[7, 30, 90].map((value) => (
              <button
                type="button"
                className={days === value ? "active" : ""}
                key={value}
                onClick={() => setDays(value)}
              >
                {value}d
              </button>
            ))}
          </div>
        </div>
      </header>

      {error && <div className="reliability-warning">{error}</div>}

      <section className="reliability-metrics">
        <article>
          <span>Run success</span>
          <strong>{formatPercent(summary.success_rate)}</strong>
          <small>{summary.terminal_run_count} terminal runs</small>
        </article>
        <article>
          <span>P95 latency</span>
          <strong>{formatDuration(summary.duration_p95_ms)}</strong>
          <small>P50 {formatDuration(summary.duration_p50_ms)}</small>
        </article>
        <article>
          <span>Tokens consumed</span>
          <strong>{formatTokens(summary.total_tokens)}</strong>
          <small>avg {formatTokens(summary.average_tokens)} / terminal run</small>
        </article>
        <article>
          <span>Tool error rate</span>
          <strong>{formatPercent(summary.tool_error_rate)}</strong>
          <small>{summary.tool_call_count} persisted calls</small>
        </article>
        <article>
          <span>Evidence coverage</span>
          <strong>{overview.coverage.runs_with_duration}/{overview.coverage.terminal_runs}</strong>
          <small>runs with measured latency</small>
        </article>
      </section>

      <div className="reliability-main-grid">
        <section className="reliability-card reliability-trend">
          <div className="reliability-section-heading">
            <div>
              <span className="reliability-eyebrow">Daily operating signal</span>
              <h2>Volume, tokens, and success</h2>
            </div>
            <div className="reliability-legend">
              <span><i className="runs" />runs</span>
              <span><i className="tokens" />tokens</span>
            </div>
          </div>
          <div className="reliability-chart">
            {trend.map((point) => (
              <div key={point.date} title={`${point.date}: ${point.runs} runs`}>
                <div className="reliability-bars">
                  <i className="runs" style={{ height: `${point.run_height}%` }} />
                  <i className="tokens" style={{ height: `${point.token_height}%` }} />
                </div>
                <strong>{point.success_rate == null ? "—" : `${Math.round(point.success_rate * 100)}%`}</strong>
                <small>{point.date.slice(5)}</small>
              </div>
            ))}
          </div>
        </section>

        <section className="reliability-card reliability-objectives">
          <span className="reliability-eyebrow">Reference objectives</span>
          <h2>Health gates</h2>
          <div>
            {overview.objectives.map((objective) => (
              <article key={objective.id} className={objective.met == null ? "unknown" : objective.met ? "met" : "missed"}>
                <i>{objective.met == null ? "?" : objective.met ? "✓" : "!"}</i>
                <div>
                  <strong>{objective.label}</strong>
                  <p>{objective.detail}</p>
                </div>
                <span>
                  {formatObjectiveValue(objective, objective.observed)}
                  <small>
                    {objective.comparator === "gte" ? "≥" : "≤"} {formatObjectiveValue(objective, objective.target)}
                  </small>
                </span>
              </article>
            ))}
          </div>
        </section>
      </div>

      <div className="reliability-secondary-grid">
        <section className="reliability-card reliability-tools">
          <div className="reliability-section-heading">
            <div>
              <span className="reliability-eyebrow">Capability dependability</span>
              <h2>Tool outcomes</h2>
            </div>
            <span>{overview.tools.length} observed tools</span>
          </div>
          <div className="reliability-tool-table">
            <div><span>Tool</span><span>Calls</span><span>Failures</span><span>Error rate</span><span>Avg latency</span></div>
            {overview.tools.slice(0, 12).map((tool) => (
              <div key={tool.name}>
                <code>{tool.name}</code>
                <span>{tool.calls}</span>
                <span>{tool.failures}</span>
                <span className={tool.error_rate > 0.05 ? "bad" : "good"}>{formatPercent(tool.error_rate)}</span>
                <span>{formatDuration(tool.average_duration_ms)}</span>
              </div>
            ))}
            {!overview.tools.length && <p>No persisted tool steps in this window.</p>}
          </div>
        </section>

        <section className="reliability-card reliability-failures">
          <span className="reliability-eyebrow">Termination taxonomy</span>
          <h2>Why runs did not complete</h2>
          <div>
            {overview.failure_reasons.map((item) => (
              <article key={item.reason}>
                <span>{item.reason.replace(/_/g, " ")}</span>
                <strong>{item.count}</strong>
              </article>
            ))}
            {!overview.failure_reasons.length && (
              <p>No non-success terminal runs in this window.</p>
            )}
          </div>
        </section>
      </div>

      <section className="reliability-card reliability-runs">
        <div className="reliability-section-heading">
          <div>
            <span className="reliability-eyebrow">Audit trail</span>
            <h2>Recent persisted runs</h2>
          </div>
          <span>{summary.active_run_count} active</span>
        </div>
        <div className="reliability-run-table">
          <div><span>Run</span><span>Agent / session</span><span>Status</span><span>Reason</span><span>Steps</span><span>Tokens</span><span>Duration</span></div>
          {overview.recent_runs.map((run) => (
            <div key={run.id}>
              <code>{run.id.slice(0, 8)}</code>
              <span title={run.session_title}>{run.agent_name} · {run.session_title}</span>
              <span className={`status-${run.status}`}>{run.status}</span>
              <span>{run.termination_reason.replace(/_/g, " ")}</span>
              <span>{run.steps}</span>
              <span>{formatTokens(run.tokens)}</span>
              <span>{formatDuration(run.duration_ms)}</span>
            </div>
          ))}
        </div>
      </section>

      <footer className="reliability-disclosure">
        These are reference engineering objectives, not production SLAs.
        Token counts are resource consumption, not currency cost. Historical
        zero-token runs remain visible rather than being estimated.
      </footer>
    </div>
  );
}
