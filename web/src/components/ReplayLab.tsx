import { useEffect, useMemo, useState } from "react";

import { getSessionReplay } from "../api/replay";
import { selectSessionUi, useChatStore } from "../stores/chatStore";
import { useSessionStore } from "../stores/sessionStore";
import type {
  ReplayRun,
  ReplayStep,
  ReplayToolVisibility,
  SessionReplay,
} from "../types/replay";
import { ViewStatePanel } from "./ViewStatePanel";
import { SessionRequiredState } from "./SessionRequiredState";

function shortId(value: string) {
  return value ? value.slice(0, 8) : "—";
}

function titleCase(value?: string) {
  return (value || "unknown")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function displayValue(value: unknown) {
  if (value === null || value === undefined || value === "") return "not recorded";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "object") {
    const serialized = JSON.stringify(value);
    return serialized === "{}" ? "not recorded" : serialized;
  }
  return String(value);
}

export function deriveVisibleToolDelta(
  current: ReplayToolVisibility[],
  previous: ReplayToolVisibility[] = [],
) {
  const before = new Set(
    previous.filter((tool) => tool.visible).map((tool) => tool.name),
  );
  const now = new Set(
    current.filter((tool) => tool.visible).map((tool) => tool.name),
  );
  return {
    added: [...now].filter((name) => !before.has(name)).sort(),
    removed: [...before].filter((name) => !now.has(name)).sort(),
    unchanged: [...now].filter((name) => before.has(name)).sort(),
  };
}

function SnapshotFacts({
  title,
  values,
}: {
  title: string;
  values: Record<string, unknown>;
}) {
  const entries = Object.entries(values || {});
  return (
    <article className="replay-snapshot-card">
      <span>{title}</span>
      {entries.length ? (
        <dl>
          {entries.map(([key, value]) => (
            <div key={key}>
              <dt>{titleCase(key)}</dt>
              <dd title={displayValue(value)}>{displayValue(value)}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <p>Not present in this record.</p>
      )}
    </article>
  );
}

function RunGate({ run }: { run: ReplayRun }) {
  const state = run.validation.valid && run.evidence_complete
    ? "verified"
    : run.validation.valid ? "partial" : "invalid";
  return (
    <section className={`replay-gate replay-gate-${state}`}>
      <div className="replay-gate-mark">
        {state === "verified" ? "✓" : state === "partial" ? "~" : "!"}
      </div>
      <div>
        <span>Replay gate</span>
        <h2>
          {state === "verified"
            ? "Contract and persisted steps agree"
            : state === "partial"
              ? "Valid historical reconstruction"
              : "Replay evidence failed validation"}
        </h2>
        <p>
          Schema {run.validation.schema_valid ? "valid" : "invalid"} · boundary{" "}
          {run.validation.boundary_preserved ? "preserved" : "violated"} ·{" "}
          {run.validation.record_step_count}/{run.validation.event_step_count} contract/event steps
        </p>
      </div>
      <div className="replay-gate-source">
        <span>{run.contract_source.replace(/_/g, " ")}</span>
        <code>v{run.record.version}</code>
      </div>
    </section>
  );
}

function CausalStage({
  index,
  label,
  title,
  body,
  state = "",
}: {
  index: number;
  label: string;
  title: string;
  body: string;
  state?: string;
}) {
  return (
    <div className={`replay-causal-stage ${state}`}>
      <i>{index}</i>
      <span>{label}</span>
      <strong>{title}</strong>
      <p>{body}</p>
    </div>
  );
}

function StepInspector({
  step,
  previous,
}: {
  step: ReplayStep;
  previous?: ReplayStep;
}) {
  const delta = deriveVisibleToolDelta(
    step.visible_tools,
    previous?.visible_tools,
  );
  const decision = step.runtime_decision;
  const action = step.model_action;
  const failed = step.tool_executions.filter((item) => !item.success).length;
  const actionName = titleCase(action.action_type);

  return (
    <>
      <section className="replay-card">
        <div className="replay-section-heading">
          <div>
            <span className="replay-eyebrow">Step {step.step} causal chain</span>
            <h2>Why the harness moved forward</h2>
          </div>
          <span className={`replay-validity ${step.validation.valid ? "valid" : "invalid"}`}>
            {step.validation.valid ? "step valid" : "step invalid"}
          </span>
        </div>
        <div className="replay-causal-chain">
          <CausalStage
            index={1}
            label="Runtime gate"
            title={titleCase(decision.action)}
            body={decision.reason || (
              decision.strip_tools ? "Tool visibility was restricted." : "Execution may continue."
            )}
            state={decision.action === "terminate" ? "terminal" : ""}
          />
          <CausalStage
            index={2}
            label="Model decision"
            title={actionName}
            body={action.thought || action.message || "No model narrative captured."}
          />
          <CausalStage
            index={3}
            label="Tool boundary"
            title={`${step.tool_executions.length} execution${step.tool_executions.length === 1 ? "" : "s"}`}
            body={failed ? `${failed} failed tool result${failed === 1 ? "" : "s"}.` : "All recorded executions succeeded."}
            state={failed ? "failed" : ""}
          />
          <CausalStage
            index={4}
            label="Outcome"
            title={titleCase(step.outcome)}
            body={step.termination_reason && step.termination_reason !== "none"
              ? `${titleCase(step.termination_reason)} → ${titleCase(step.termination_status)}`
              : "The next step remained inside the declared harness."}
          />
        </div>
      </section>

      <div className="replay-detail-grid">
        <section className="replay-card">
          <div className="replay-section-heading">
            <div>
              <span className="replay-eyebrow">Capability snapshot</span>
              <h2>Visible tools</h2>
            </div>
            <strong>{step.visible_tools.filter((tool) => tool.visible).length}</strong>
          </div>
          <div className="replay-tool-delta">
            {delta.added.map((name) => <code className="added" key={name}>+ {name}</code>)}
            {delta.removed.map((name) => <code className="removed" key={name}>− {name}</code>)}
            {delta.unchanged.map((name) => <code key={name}>{name}</code>)}
            {!step.visible_tools.length && <p>No visibility snapshot was recorded.</p>}
          </div>
          <div className="replay-decision-facts">
            <span>Strip tools<strong>{decision.strip_tools ? "yes" : "no"}</strong></span>
            <span>Injected message<strong>{decision.inject_message ? "present" : "none"}</strong></span>
            <span>Termination<strong>{titleCase(decision.terminate_reason)}</strong></span>
          </div>
        </section>

        <section className="replay-card">
          <div className="replay-section-heading">
            <div>
              <span className="replay-eyebrow">Normalized evidence</span>
              <h2>Tool executions</h2>
            </div>
          </div>
          <div className="replay-executions">
            {step.tool_executions.map((execution, index) => (
              <article
                className={execution.success ? "success" : "failure"}
                key={`${execution.tool_call_id || execution.tool_name}-${index}`}
              >
                <div>
                  <span className="replay-execution-status" />
                  <code>{execution.tool_name}</code>
                  <em>{Math.round(execution.duration_ms || 0)} ms</em>
                </div>
                <small>{titleCase(execution.outcome)}</small>
                <pre>{displayValue(execution.params || {})}</pre>
                {(execution.error || execution.output_summary) && (
                  <p>{execution.error || execution.output_summary}</p>
                )}
              </article>
            ))}
            {!step.tool_executions.length && (
              <p className="replay-empty-inline">No tool execution in this step.</p>
            )}
          </div>
        </section>
      </div>

      {!!step.validation.issues.length && (
        <section className="replay-card replay-issues">
          <span className="replay-eyebrow">Step validation findings</span>
          {step.validation.issues.map((issue, index) => (
            <div key={`${issue.field}-${index}`}>
              <strong>{issue.severity}</strong><code>{issue.field}</code><span>{issue.message}</span>
            </div>
          ))}
        </section>
      )}
    </>
  );
}

interface ReplayLabProps {
  requestedRunId?: string;
  onSelectRun?: (runId: string, turnId: string) => void;
}

export function ReplayLab({
  requestedRunId,
  onSelectRun,
}: ReplayLabProps = {}) {
  const activeId = useSessionStore((state) => state.activeId);
  const liveTerminal = useChatStore((state) => {
    if (!activeId) return "";
    const events = selectSessionUi(state, activeId).events;
    const terminal = events.find((event) => event.type === "run_terminal");
    return terminal ? `${terminal.run_id}:${terminal.sequence || 0}` : "";
  });
  const [data, setData] = useState<SessionReplay | null>(null);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [selectedStep, setSelectedStep] = useState(0);
  const [failureFilter, setFailureFilter] = useState("all");
  const [reloadKey, setReloadKey] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!activeId) {
      setData(null);
      setSelectedRunId("");
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError("");
    getSessionReplay(activeId, controller.signal)
      .then((next) => {
        setData(next);
        setSelectedRunId((current) => (
          requestedRunId && next.runs.some((run) => run.run_id === requestedRunId)
            ? requestedRunId
            : next.runs.some((run) => run.run_id === current)
            ? current : (next.runs[0]?.run_id || "")
        ));
      })
      .catch((reason) => {
        if (reason?.name !== "AbortError") {
          setError(reason instanceof Error ? reason.message : String(reason));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [activeId, liveTerminal, reloadKey, requestedRunId]);

  const selectedRun = useMemo(
    () => requestedRunId
      ? data?.runs.find((run) => run.run_id === requestedRunId) || null
      : data?.runs.find((run) => run.run_id === selectedRunId)
        || data?.runs[0]
        || null,
    [data, requestedRunId, selectedRunId],
  );
  const step = selectedRun?.record.steps[selectedStep] || selectedRun?.record.steps[0];
  const failureCategories = Array.from(
    new Set(data?.failure_taxonomy.map((item) => item.category) || []),
  ).sort();
  const failureRows = (data?.failure_taxonomy || []).filter(
    (item) => failureFilter === "all" || item.category === failureFilter,
  );

  useEffect(() => setSelectedStep(0), [selectedRunId]);

  if (!activeId) {
    return (
      <SessionRequiredState
        mark="RP"
        title="Select a session to replay its harness decisions"
        description="Replay uses persisted step contracts, not reconstructed chat prose."
      />
    );
  }

  if (loading && !data) {
    return (
      <ViewStatePanel
        tone="loading"
        title="Loading replay contracts"
        description="Reading persisted run and step control decisions."
      />
    );
  }
  if (error && !data) {
    return (
      <ViewStatePanel
        tone="error"
        title="Replay evidence could not be loaded"
        description={error}
      />
    );
  }
  if (!data) return null;

  return (
    <div className="replay-lab">
      <header className="replay-hero">
        <div>
          <span className="replay-eyebrow">Harness evidence</span>
          <h1>Replay & Failure Lab</h1>
          <p>
            Step through the persisted control decisions, tool boundary, and
            terminal policy for session <code>{shortId(data.session_id)}</code>.
          </p>
        </div>
        <button type="button" onClick={() => setReloadKey((value) => value + 1)}>
          {loading ? "Refreshing…" : "Refresh evidence"}
        </button>
      </header>

      {error && <div className="replay-banner-error">{error}</div>}

      <section className="replay-metrics">
        <article><strong>{data.summary.run_count}</strong><span>recorded runs</span></article>
        <article><strong>{data.summary.contract_count}</strong><span>full contracts</span></article>
        <article><strong>{data.summary.valid_count}</strong><span>valid records</span></article>
        <article><strong>{data.summary.step_count}</strong><span>replay steps</span></article>
        <article><strong>{data.summary.failed_tool_count}</strong><span>failed tools</span></article>
      </section>

      {!data.runs.length ? (
        <section className="replay-card replay-no-runs">
          <strong>No replay evidence yet</strong>
          <p>Run the agent once. New runs now emit both step and run-level contracts.</p>
        </section>
      ) : requestedRunId && !selectedRun ? (
        <section className="replay-card replay-no-runs">
          <strong>The requested run has no replay evidence in this session</strong>
          <p><code>{requestedRunId}</code></p>
        </section>
      ) : selectedRun && (
        <>
          <nav className="replay-run-strip" aria-label="Replay runs">
            {data.runs.map((run) => (
              <button
                type="button"
                className={run.run_id === selectedRun.run_id ? "active" : ""}
                key={run.run_id}
                onClick={() => {
                  setSelectedRunId(run.run_id);
                  onSelectRun?.(run.run_id, run.turn_id);
                }}
              >
                <i className={run.validation.valid ? "valid" : "invalid"} />
                <span>Run {run.turn_index + 1}</span>
                <code>{shortId(run.run_id)}</code>
                <small>{titleCase(run.status)}</small>
              </button>
            ))}
          </nav>

          <RunGate run={selectedRun} />

          <section className="replay-card replay-provenance">
            <div className="replay-section-heading">
              <div>
                <span className="replay-eyebrow">Reproduction envelope</span>
                <h2>What governed this run</h2>
              </div>
              <code>{selectedRun.run_id}</code>
            </div>
            <div className="replay-snapshot-grid">
              <SnapshotFacts title="Provenance" values={selectedRun.record.provenance || {}} />
              <SnapshotFacts title="Runtime" values={selectedRun.record.runtime_snapshot || {}} />
              <SnapshotFacts title="Permission" values={selectedRun.record.permission_snapshot || {}} />
            </div>
          </section>

          <section className="replay-card replay-step-navigation">
            <div>
              <span className="replay-eyebrow">Step-oriented record</span>
              <h2>{selectedRun.record.steps.length} decisions</h2>
            </div>
            <div className="replay-step-rail">
              {selectedRun.record.steps.map((item, index) => (
                <button
                  type="button"
                  key={`${item.step}-${index}`}
                  className={selectedStep === index ? "active" : ""}
                  onClick={() => setSelectedStep(index)}
                >
                  <i className={item.validation.valid ? "valid" : "invalid"} />
                  <span>{item.step}</span>
                  <small>{titleCase(item.model_action.action_type || item.runtime_decision.action)}</small>
                </button>
              ))}
            </div>
          </section>

          {step && (
            <StepInspector
              step={step}
              previous={selectedRun.record.steps[selectedStep - 1]}
            />
          )}

          {!!selectedRun.validation.issues.length && (
            <section className="replay-card replay-issues">
              <span className="replay-eyebrow">Run validation findings</span>
              {selectedRun.validation.issues.map((issue, index) => (
                <div key={`${issue.field}-${index}`}>
                  <strong>{issue.severity}</strong><code>{issue.field}</code><span>{issue.message}</span>
                </div>
              ))}
            </section>
          )}
        </>
      )}

      <section className="replay-card replay-taxonomy">
        <div className="replay-section-heading">
          <div>
            <span className="replay-eyebrow">Fail-closed policy</span>
            <h2>Failure boundary matrix</h2>
          </div>
          <select
            value={failureFilter}
            onChange={(event) => setFailureFilter(event.target.value)}
          >
            <option value="all">All categories</option>
            {failureCategories.map((category) => (
              <option value={category} key={category}>{titleCase(category)}</option>
            ))}
          </select>
        </div>
        <div className="replay-taxonomy-table">
          <div><span>Reason</span><span>Category</span><span>Behavior</span><span>Expected status</span><span>Recovery</span></div>
          {failureRows.map((item) => (
            <div key={item.reason}>
              <code>{item.reason}</code>
              <span>{titleCase(item.category)}</span>
              <span>{titleCase(item.behavior)}</span>
              <strong>{titleCase(item.expected_status)}</strong>
              <span>{item.max_recovery_attempts ? `${item.max_recovery_attempts} attempt` : "none"}</span>
            </div>
          ))}
        </div>
      </section>

      <footer className="replay-disclosure">
        Source: {data.disclosure.source}. Historical reconstructions never
        invent provenance. Tool outputs are truncated by the runtime contract.
      </footer>
    </div>
  );
}
