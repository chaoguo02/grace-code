import { useEffect, useMemo, useState } from "react";
import { getEvaluationOverview } from "../api/evaluations";
import { useSessionStore } from "../stores/sessionStore";
import type {
  EvaluationOverview,
  EvaluationRun,
} from "../types/evaluations";
import { ViewStatePanel } from "./ViewStatePanel";

const VALIDATION_COMMAND = "python scripts/run_langfuse_validation.py --repo . --scenario both";

export interface EvaluationTrendPoint {
  id: string;
  label: string;
  passPercent: number;
  tokenPercent: number;
  averageTokens: number;
  regressed: boolean;
}

export function deriveEvaluationTrend(
  runs: EvaluationRun[],
): EvaluationTrendPoint[] {
  const ordered = [...runs].slice(0, 12).reverse();
  const maxTokens = Math.max(
    1,
    ...ordered.map((run) => run.average_tokens || 0),
  );
  return ordered.map((run) => ({
    id: run.id,
    label: run.label,
    passPercent: Math.max(0, Math.min(100, run.pass_rate * 100)),
    tokenPercent: Math.max(
      2,
      Math.min(100, (run.average_tokens / maxTokens) * 100),
    ),
    averageTokens: run.average_tokens,
    regressed: Boolean(
      run.comparison
      && run.comparison.checks.some((check) => !check.passed),
    ),
  }));
}

function formatPercent(value?: number | null, signed = false) {
  if (value == null || !Number.isFinite(value)) return "—";
  const percent = value * 100;
  const prefix = signed && percent > 0 ? "+" : "";
  return `${prefix}${percent.toFixed(percent % 1 === 0 ? 0 : 1)}%`;
}

function formatTokens(value?: number) {
  const tokens = value || 0;
  return tokens >= 1000 ? `${(tokens / 1000).toFixed(1)}k` : tokens.toLocaleString();
}

function formatDate(value?: string) {
  if (!value) return "Unknown";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
}

function EmptyEvaluation({
  scenarios,
}: {
  scenarios: EvaluationOverview["scenario_catalog"];
}) {
  const [copied, setCopied] = useState(false);
  const copyCommand = async () => {
    try {
      await navigator.clipboard.writeText(VALIDATION_COMMAND);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="eval-empty-layout">
      <section className="eval-empty-callout">
        <div className="eval-empty-mark">E</div>
        <div>
          <span className="summary-label">No evaluation artifacts yet</span>
          <h3>Run the existing validation suite</h3>
          <p>
            The lab reads structured CLI/CI artifacts. A completed chat session
            is intentionally not treated as an evaluation pass.
          </p>
          <div className="eval-command">
            <code>{VALIDATION_COMMAND}</code>
            <button type="button" onClick={copyCommand}>
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
        </div>
      </section>

      <section className="eval-scenario-catalog">
        <div className="eval-section-head">
          <div>
            <span className="summary-label">Scenario contracts</span>
            <h3>What will be evaluated</h3>
          </div>
          <span>{scenarios.length} cases</span>
        </div>
        <div className="eval-scenario-grid">
          {scenarios.map((scenario) => (
            <article key={scenario.name}>
              <div>
                <span>{scenario.mode}</span>
                <strong>{scenario.name}</strong>
              </div>
              <p>{scenario.description}</p>
              <dl>
                <div><dt>Expected</dt><dd>{scenario.expected_status}</dd></div>
                <div><dt>Steps</dt><dd>{scenario.max_steps}</dd></div>
                <div><dt>Budget</dt><dd>{formatTokens(scenario.budget_tokens)}</dd></div>
                <div>
                  <dt>Failure data</dt>
                  <dd>{scenario.expect_failure_dataset_increment ? "Required" : "Must stay clean"}</dd>
                </div>
              </dl>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}

interface EvaluationLabProps {
  onNavigate: (view: "runs" | "context") => void;
  onOpenHealth?: () => void;
}

export function EvaluationLab({
  onNavigate,
  onOpenHealth,
}: EvaluationLabProps) {
  const [overview, setOverview] = useState<EvaluationOverview | null>(null);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    getEvaluationOverview(controller.signal)
      .then((next) => {
        setOverview(next);
        setSelectedRunId((current) => (
          current && next.runs.some((run) => run.id === current)
            ? current
            : next.runs[0]?.id || ""
        ));
        setError("");
      })
      .catch((reason) => {
        if (controller.signal.aborted) return;
        setError(reason instanceof Error ? reason.message : "Unable to load evaluation artifacts");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  const selectedRun = overview?.runs.find((run) => run.id === selectedRunId)
    || overview?.runs[0];
  const trend = useMemo(
    () => deriveEvaluationTrend(overview?.runs || []),
    [overview?.runs],
  );
  const failedChecks = selectedRun?.comparison?.checks.filter(
    (check) => !check.passed,
  ) || [];

  const openInspector = (
    sessionId: string,
    view: "runs" | "context",
  ) => {
    useSessionStore.getState().openSession(sessionId);
    onNavigate(view);
  };

  return (
    <section className="view active" data-view-name="evaluations">
      <div className="eval-page">
        <header className="eval-hero">
          <div>
            <span className="summary-label">Evaluation & Regression Lab</span>
            <h2>Prove quality, cost, and stability</h2>
            <p>
              Contract-based validation results and baseline comparisons from
              the project&apos;s existing Langfuse evaluation pipeline.
            </p>
          </div>
          <div className="quality-header-actions">
            {onOpenHealth && (
              <button type="button" onClick={onOpenHealth}>
                Open health
              </button>
            )}
            <div className="eval-hero-badge">
              <span>Read-only artifact view</span>
              <strong>CLI / CI is the execution owner</strong>
            </div>
          </div>
        </header>

        {error && overview && <div className="eval-error">{error}</div>}
        {loading && !overview ? (
          <ViewStatePanel
            tone="loading"
            title="Loading evaluation artifacts"
            description="Reading structured CLI and CI validation reports."
          />
        ) : error && !overview ? (
          <ViewStatePanel
            tone="error"
            title="Evaluation artifacts could not be loaded"
            description={error}
          />
        ) : overview && overview.runs.length === 0 ? (
          <EmptyEvaluation scenarios={overview.scenario_catalog} />
        ) : overview && selectedRun ? (
          <>
            <section className="eval-metrics">
              <div>
                <span>Evaluation runs</span>
                <strong>{overview.summary.run_count}</strong>
                <small>Structured reports discovered</small>
              </div>
              <div>
                <span>Latest pass rate</span>
                <strong>{formatPercent(overview.summary.latest_pass_rate)}</strong>
                <small>
                  {overview.summary.pass_rate_delta == null
                    ? "No previous run"
                    : `${formatPercent(overview.summary.pass_rate_delta, true)} vs previous`}
                </small>
              </div>
              <div>
                <span>Average tokens</span>
                <strong>{formatTokens(overview.summary.latest_average_tokens)}</strong>
                <small>
                  {overview.summary.token_delta_pct == null
                    ? "No previous run"
                    : `${formatPercent(overview.summary.token_delta_pct, true)} vs previous`}
                </small>
              </div>
              <div className={overview.summary.regression_count > 0 ? "bad" : "good"}>
                <span>Regression checks</span>
                <strong>{overview.summary.regression_count}</strong>
                <small>
                  {overview.summary.regression_count > 0
                    ? "Failed checks need review"
                    : "No regression detected"}
                </small>
              </div>
            </section>

            <div className="eval-main-grid">
              <section className="eval-card eval-trend-card">
                <div className="eval-section-head">
                  <div>
                    <span className="summary-label">Run history</span>
                    <h3>Pass rate and token cost</h3>
                  </div>
                  <div className="eval-chart-legend">
                    <span><i className="pass" /> Pass rate</span>
                    <span><i className="tokens" /> Relative tokens</span>
                  </div>
                </div>
                <div className="eval-trend-chart">
                  {trend.map((point) => (
                    <button
                      key={point.id}
                      type="button"
                      className={`${point.id === selectedRun.id ? "selected" : ""} ${point.regressed ? "regressed" : ""}`}
                      onClick={() => setSelectedRunId(point.id)}
                      title={`${point.label}: ${point.passPercent.toFixed(0)}% pass, ${Math.round(point.averageTokens)} avg tokens`}
                    >
                      <span
                        className="eval-trend-token"
                        style={{ height: `${point.tokenPercent}%` }}
                      />
                      <span
                        className="eval-trend-pass"
                        style={{ height: `${point.passPercent}%` }}
                      />
                      <small>{point.label.slice(4, 12) || point.label}</small>
                    </button>
                  ))}
                </div>
              </section>

              <aside className="eval-card eval-run-picker">
                <div className="eval-section-head">
                  <div>
                    <span className="summary-label">Selected run</span>
                    <h3>{selectedRun.label}</h3>
                  </div>
                  <span className={selectedRun.all_passed ? "eval-verdict pass" : "eval-verdict fail"}>
                    {selectedRun.all_passed ? "Passed" : "Failed"}
                  </span>
                </div>
                <select
                  value={selectedRun.id}
                  onChange={(event) => setSelectedRunId(event.target.value)}
                >
                  {overview.runs.map((run) => (
                    <option key={run.id} value={run.id}>
                      {formatDate(run.created_at)} · {formatPercent(run.pass_rate)}
                    </option>
                  ))}
                </select>
                <dl className="eval-run-facts">
                  <div><dt>Created</dt><dd>{formatDate(selectedRun.created_at)}</dd></div>
                  <div><dt>Scenarios</dt><dd>{selectedRun.passed_count}/{selectedRun.scenario_count}</dd></div>
                  <div><dt>Average steps</dt><dd>{selectedRun.average_steps.toFixed(1)}</dd></div>
                  <div><dt>Artifact</dt><dd title={selectedRun.path}>{selectedRun.path}</dd></div>
                  <div><dt>Provider</dt><dd>{selectedRun.configuration.provider || "Not recorded"}</dd></div>
                  <div><dt>Model</dt><dd>{selectedRun.configuration.model || "Not recorded"}</dd></div>
                  <div><dt>Prompt</dt><dd>{selectedRun.configuration.prompt_label || "Not recorded"}</dd></div>
                  <div><dt>Version</dt><dd>{selectedRun.configuration.prompt_version ?? "—"}</dd></div>
                </dl>
              </aside>
            </div>

            <section className="eval-card">
              <div className="eval-section-head">
                <div>
                  <span className="summary-label">Scenario matrix</span>
                  <h3>Expected outcome versus observed outcome</h3>
                </div>
                <span>{selectedRun.results.length} results</span>
              </div>
              <div className="eval-result-table">
                <div className="eval-result-row header">
                  <span>Scenario</span>
                  <span>Expected</span>
                  <span>Actual</span>
                  <span>Steps</span>
                  <span>Tokens</span>
                  <span>Evidence</span>
                </div>
                {selectedRun.results.map((result) => (
                  <div
                    key={result.scenario}
                    className={`eval-result-row ${result.passed ? "passed" : "failed"}`}
                  >
                    <div>
                      <i>{result.passed ? "✓" : "!"}</i>
                      <span>
                        <strong>{result.scenario}</strong>
                        <small>{result.summary}</small>
                      </span>
                    </div>
                    <span>{result.expected_status}</span>
                    <span>{result.actual_status}</span>
                    <span>{result.steps}</span>
                    <span>{formatTokens(result.tokens)}</span>
                    <div className="eval-result-actions">
                      {result.trace_url && (
                        <a href={result.trace_url} target="_blank" rel="noreferrer">
                          Trace
                        </a>
                      )}
                      {result.session_id && (
                        <>
                          <button
                            type="button"
                            onClick={() => openInspector(result.session_id!, "runs")}
                          >
                            Run
                          </button>
                          <button
                            type="button"
                            onClick={() => openInspector(result.session_id!, "context")}
                          >
                            Context
                          </button>
                        </>
                      )}
                      {!result.trace_url && !result.session_id && <span>Artifact only</span>}
                    </div>
                  </div>
                ))}
              </div>
            </section>

            <div className="eval-main-grid">
              <section className="eval-card">
                <div className="eval-section-head">
                  <div>
                    <span className="summary-label">Regression comparison</span>
                    <h3>
                      {selectedRun.comparison
                        ? selectedRun.comparison.passed
                          ? "Baseline checks passed"
                          : `${failedChecks.length} checks failed`
                        : "No baseline comparison"}
                    </h3>
                  </div>
                  {selectedRun.comparison_source && (
                    <span>{selectedRun.comparison_source} comparison</span>
                  )}
                </div>
                {!selectedRun.comparison ? (
                  <p className="eval-muted">
                    Add a baseline JSON under ci/langfuse-baselines to enable
                    automatic status and Token regression checks.
                  </p>
                ) : (
                  <div className="eval-check-list">
                    {selectedRun.comparison.checks.map((check) => (
                      <article key={check.name} className={check.passed ? "passed" : "failed"}>
                        <i>{check.passed ? "✓" : "!"}</i>
                        <div>
                          <strong>{check.name.replace(/_/g, " ")}</strong>
                          <small>{check.details}</small>
                        </div>
                      </article>
                    ))}
                  </div>
                )}
              </section>

              <aside className="eval-card">
                <div className="eval-section-head">
                  <div>
                    <span className="summary-label">Baselines</span>
                    <h3>Reference configurations</h3>
                  </div>
                  <span>{overview.baselines.length}</span>
                </div>
                {overview.baselines.length === 0 ? (
                  <p className="eval-muted">
                    No stable baseline is registered yet.
                  </p>
                ) : (
                  <div className="eval-baseline-list">
                    {overview.baselines.map((baseline) => (
                      <article key={`${baseline.id}-${baseline.path}`}>
                        <div>
                          <strong>{baseline.name}</strong>
                          <small>{formatDate(baseline.created_at)}</small>
                        </div>
                        <span>{formatPercent(baseline.pass_rate)}</span>
                        <span>{formatTokens(baseline.average_tokens)} tok</span>
                      </article>
                    ))}
                  </div>
                )}
              </aside>
            </div>

            <footer className="eval-disclosure">
              <strong>Evaluation boundary</strong>
              <span>
                Pass/fail comes from scenario contracts, trace capture, and
                failure-dataset behavior—not from the Session completion state.
              </span>
            </footer>
          </>
        ) : null}
      </div>
    </section>
  );
}
