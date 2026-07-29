import { useCallback, useEffect, useMemo, useState } from "react";
import {
  cancelDelegationRun,
  cancelDelegationTask,
  getDelegationRun,
  integrateDelegationRun,
  resumeDelegationRun,
  retryDelegationTask,
  verifyDelegationRun,
  type DelegationRunDetail,
} from "../api/multiAgent";
import type { DelegationRunState, DelegationRuns, TaskState } from "../types/delegation";

interface MultiAgentRunCardProps {
  sessionId?: string | null;
  runs: DelegationRuns;
  onViewChild: (childSessionId: string) => void;
}

type JsonObject = Record<string, unknown>;
type IntegrationAction = "apply" | "discard" | "retain";

function object(value: unknown): JsonObject {
  return value && typeof value === "object" ? value as JsonObject : {};
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function number(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

function readable(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function latestRun(runs: DelegationRuns): DelegationRunState | null {
  const values = Object.values(runs);
  if (!values.length) return null;
  return values.reduce((latest, run) => {
    const latestSequence = latest.updatedSequence ?? -1;
    const runSequence = run.updatedSequence ?? -1;
    if (runSequence !== latestSequence) return runSequence > latestSequence ? run : latest;
    return (run.updatedAt || run.createdAt || "") >= (latest.updatedAt || latest.createdAt || "")
      ? run
      : latest;
  });
}

function taskCounts(tasks: Array<{ status: string }>) {
  return tasks.reduce(
    (counts, task) => {
      if (task.status === "running") counts.running += 1;
      else if (task.status === "queued") counts.queued += 1;
      else if (["completed", "no_findings"].includes(task.status)) counts.completed += 1;
      else counts.failed += 1;
      return counts;
    },
    { completed: 0, running: 0, failed: 0, queued: 0 },
  );
}

function effectiveTask(raw: JsonObject, live?: TaskState) {
  const report = object(raw.report);
  const worktree = object(report.worktree);
  const reportFiles = Array.isArray(report.changed_files)
    ? report.changed_files.map((item) => text(object(item).path)).filter(Boolean)
    : [];
  const changedFiles = reportFiles.length ? reportFiles : strings(worktree.changed_files);
  return {
    id: text(raw.id, live?.taskId || "unknown"),
    goal: text(raw.goal, live?.taskId || "Untitled task"),
    agentType: text(raw.agent_type, live?.agentType || "agent"),
    childSessionId: text(raw.child_session_id, live?.childSessionId || ""),
    status: text(raw.status, live?.status || "queued"),
    required: raw.required !== false,
    dependencies: strings(raw.dependencies).length ? strings(raw.dependencies) : live?.dependencies || [],
    retryCount: number(raw.retry_count, live?.generation || 0),
    maxRetries: number(raw.max_retries, 0),
    integrationStatus: text(raw.integration_status, live?.integrationStatus || "not_required"),
    integrationError: text(raw.integration_error),
    reportSummary: text(report.summary),
    unresolved: strings(report.unresolved),
    warnings: strings(report.warnings),
    changedFiles,
    revision: text(worktree.revision),
    tokensUsed: number(report.tokens_used, live?.tokensUsed || 0),
    durationMs: number(report.duration_ms, live?.durationMs || 0),
  };
}

export function MultiAgentRunCard({ sessionId, runs, onViewChild }: MultiAgentRunCardProps) {
  const liveRun = useMemo(() => latestRun(runs), [runs]);
  const [detail, setDetail] = useState<DelegationRunDetail | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [decisions, setDecisions] = useState<Record<string, IntegrationAction | "">>({});
  const [busy, setBusy] = useState("");
  const [actionError, setActionError] = useState("");

  const loadDetail = useCallback(async () => {
    if (!sessionId || !liveRun) return;
    const value = await getDelegationRun(sessionId, liveRun.runId);
    setDetail(value);
  }, [sessionId, liveRun?.runId]);

  useEffect(() => {
    const controller = new AbortController();
    if (sessionId && liveRun) {
      getDelegationRun(sessionId, liveRun.runId, controller.signal)
        .then(setDetail)
        .catch((error) => {
          if (error?.name !== "AbortError") setActionError(String(error?.message || error));
        });
    } else {
      setDetail(null);
    }
    return () => controller.abort();
  }, [sessionId, liveRun?.runId, liveRun?.updatedSequence]);

  const execute = async (label: string, action: () => Promise<unknown>) => {
    setBusy(label);
    setActionError("");
    try {
      await action();
      await loadDetail();
    } catch (error) {
      setActionError(String((error as Error)?.message || error));
    } finally {
      setBusy("");
    }
  };

  if (!liveRun) {
    return (
      <section className="multi-agent-run-card empty" aria-label="Multi-agent delegation status">
        <header className="multi-agent-run-header">
          <div><div className="multi-agent-run-eyebrow">Multi-Agent Run</div><div className="multi-agent-run-title">Awaiting delegation</div></div>
          <div className="multi-agent-run-state" data-status="planned"><span>Ready</span><strong>Not started</strong></div>
        </header>
      </section>
    );
  }

  const rawRun = object(detail?.run);
  const status = text(rawRun.status, liveRun.status);
  const phase = text(rawRun.phase, liveRun.phase);
  const rawTasks = detail?.tasks || [];
  const tasks = rawTasks.length
    ? rawTasks.map((item) => effectiveTask(object(item), liveRun.tasks[text(object(item).id)]))
    : Object.values(liveRun.tasks).map((task) => effectiveTask({}, task));
  const counts = taskCounts(tasks);
  const pendingIntegration = tasks.filter((task) => ["pending", "retained"].includes(task.integrationStatus));
  const verification = object(rawRun.verification || liveRun.verification);
  const canResume = phase === "recovery_required";
  const canVerify = ["awaiting_verification", "verification_failed"].includes(phase);
  const terminal = ["completed", "partial", "failed", "cancelled"].includes(status);
  const allDecided = pendingIntegration.length > 0 && pendingIntegration.every(
    (task) => decisions[task.id] && task.revision,
  );

  return (
    <section className="multi-agent-run-card" aria-label="Multi-agent delegation status">
      <header className="multi-agent-run-header">
        <div>
          <div className="multi-agent-run-eyebrow">Multi-Agent Run</div>
          <div className="multi-agent-run-title">{readable(liveRun.topology)}</div>
          <div className="multi-agent-run-id">{liveRun.runId}</div>
        </div>
        <div className="multi-agent-run-state" data-status={status}><span>{readable(phase)}</span><strong>{readable(status)}</strong></div>
      </header>

      <div className="multi-agent-run-counts" aria-label={`${Math.max(liveRun.taskCount, tasks.length)} tasks`}>
        <span><strong>{counts.completed}</strong> completed</span>
        <span><strong>{counts.running}</strong> running</span>
        <span><strong>{counts.failed}</strong> blocked/failed</span>
        <span><strong>{counts.queued}</strong> queued</span>
      </div>

      {actionError && <div className="multi-agent-action-error" role="alert">{actionError}</div>}

      <div className="multi-agent-task-list">
        {tasks.map((task) => {
          const retryable = ["failed", "partial", "cancelled", "interrupted", "budget_exhausted", "blocked"].includes(task.status)
            && task.retryCount < task.maxRetries;
          const active = ["queued", "running"].includes(task.status);
          const open = Boolean(expanded[task.id]);
          return (
            <article className="multi-agent-task" key={task.id} data-status={task.status}>
              <div className="multi-agent-task-row">
                <button type="button" className="multi-agent-task-toggle" onClick={() => setExpanded((value) => ({ ...value, [task.id]: !open }))} aria-expanded={open}>
                  <span className="multi-agent-task-agent">{readable(task.agentType)}</span>
                  <span className="multi-agent-task-title">{task.goal}</span>
                  <span className="multi-agent-task-status">{readable(task.status)}</span>
                </button>
                {task.childSessionId && !task.childSessionId.includes(":") && (
                  <button type="button" className="multi-agent-task-detail" onClick={() => onViewChild(task.childSessionId)}>Child</button>
                )}
                {retryable && sessionId && (
                  <button type="button" className="multi-agent-task-detail" disabled={Boolean(busy)} onClick={() => void execute(`retry:${task.id}`, () => retryDelegationTask(sessionId, task.id))}>Retry</button>
                )}
                {active && sessionId && (
                  <button type="button" className="multi-agent-task-detail danger" disabled={Boolean(busy)} onClick={() => void execute(`cancel:${task.id}`, () => cancelDelegationTask(sessionId, task.id))}>Cancel</button>
                )}
              </div>
              <div className="multi-agent-task-dependencies">
                {task.dependencies.length ? <>Depends on {task.dependencies.map((dependency) => <code key={dependency}>{dependency}</code>)}</> : "Root task"}
                {task.required ? <span>Required</span> : <span>Optional</span>}
              </div>
              {open && (
                <div className="multi-agent-task-report">
                  <p>{task.reportSummary || "No worker report has been persisted yet."}</p>
                  {task.changedFiles.length > 0 && <div><strong>Changed files</strong><ul>{task.changedFiles.map((file) => <li key={file}><code>{file}</code></li>)}</ul></div>}
                  {task.unresolved.length > 0 && <div><strong>Unresolved</strong><ul>{task.unresolved.map((item) => <li key={item}>{item}</li>)}</ul></div>}
                  {task.warnings.length > 0 && <div><strong>Warnings</strong><ul>{task.warnings.map((item) => <li key={item}>{item}</li>)}</ul></div>}
                  <div className="multi-agent-task-meta"><span>Integration: {readable(task.integrationStatus)}</span><span>{task.tokensUsed.toLocaleString()} tokens</span><span>{task.durationMs} ms</span></div>
                  {task.integrationError && <div className="multi-agent-action-error">{task.integrationError}</div>}
                </div>
              )}
              {["pending", "retained"].includes(task.integrationStatus) && (
                <div className="multi-agent-integration-decision">
                  <label>Reviewed decision
                    <select value={decisions[task.id] || ""} onChange={(event) => setDecisions((value) => ({ ...value, [task.id]: event.target.value as IntegrationAction | "" }))}>
                      <option value="">Choose…</option><option value="apply">Apply</option><option value="discard">Discard</option><option value="retain">Retain</option>
                    </select>
                  </label>
                  <code title="Expected worktree revision">{task.revision || "Revision unavailable"}</code>
                </div>
              )}
            </article>
          );
        })}
      </div>

      {(pendingIntegration.length > 0 || Object.keys(verification).length > 0) && (
        <div className="multi-agent-gates">
          <div><strong>Integration</strong><span>{pendingIntegration.length ? `${pendingIntegration.length} reviewed decisions required` : "Converged"}</span></div>
          <div><strong>Verification</strong><span>{readable(text(verification.status, "not run"))}</span></div>
        </div>
      )}

      {sessionId && (
        <footer className="multi-agent-run-actions">
          {pendingIntegration.length > 0 && <button type="button" className="btn-secondary" disabled={!allDecided || Boolean(busy)} onClick={() => void execute("integrate", () => integrateDelegationRun(sessionId, liveRun.runId, pendingIntegration.map((task) => ({ task_id: task.id, action: decisions[task.id] as IntegrationAction, expected_revision: task.revision }))))}>Integrate reviewed worktrees</button>}
          {canVerify && <button type="button" className="btn-secondary" disabled={Boolean(busy)} onClick={() => void execute("verify", () => verifyDelegationRun(sessionId, liveRun.runId))}>Run verification</button>}
          {canResume && <button type="button" className="btn-secondary" disabled={Boolean(busy)} onClick={() => void execute("resume", () => resumeDelegationRun(sessionId, liveRun.runId))}>Resume interrupted DAG</button>}
          {!terminal && <button type="button" className="btn-secondary danger" disabled={Boolean(busy)} onClick={() => { if (window.confirm("Cancel this Multi-Agent run? Active child tasks will be stopped.")) void execute("cancel-run", () => cancelDelegationRun(sessionId, liveRun.runId)); }}>Cancel run</button>}
          {busy && <span className="multi-agent-busy">Working…</span>}
        </footer>
      )}
    </section>
  );
}
