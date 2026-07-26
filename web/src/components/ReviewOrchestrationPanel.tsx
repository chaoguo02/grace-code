import { useEffect, useMemo, useState } from "react";
import type {
  ReviewFinding,
  ReviewJob,
  ReviewTask,
  ReviewTaskAttempt,
} from "../api/reviews";

type StageState = "pending" | "active" | "complete" | "warning";

export interface ReviewStage {
  id: "snapshot" | "dispatch" | "parallel" | "aggregate" | "result";
  label: string;
  detail: string;
  state: StageState;
}

export interface ReviewOrchestration {
  stages: ReviewStage[];
  completedTasks: number;
  activeTasks: number;
  affectedTasks: number;
  severity: Record<ReviewFinding["severity"], number>;
  verifiedFindings: number;
  corroboratedFindings: number;
}

const ACTIVE_JOB_STATES = ["queued", "running", "aggregating", "cancelling"];
const TERMINAL_JOB_STATES = ["completed", "partial", "stale", "failed", "cancelled"];
const TERMINAL_TASK_STATES = ["completed", "partial", "failed", "cancelled"];

function isActiveJob(status: string) {
  return ACTIVE_JOB_STATES.includes(status);
}

function isTerminalJob(status: string) {
  return TERMINAL_JOB_STATES.includes(status);
}

function stageState(
  complete: boolean,
  active: boolean,
  warning = false,
): StageState {
  if (warning) return "warning";
  if (complete) return "complete";
  if (active) return "active";
  return "pending";
}

export function deriveReviewOrchestration(job: ReviewJob): ReviewOrchestration {
  const findings = job.result.findings || [];
  const completedTasks = job.tasks.filter(
    (task) => task.status === "completed",
  ).length;
  const activeTasks = job.tasks.filter(
    (task) => task.status === "running" || task.status === "queued",
  ).length;
  const affectedTasks = job.tasks.filter(
    (task) => ["partial", "failed", "cancelled"].includes(task.status),
  ).length;
  const terminal = isTerminalJob(job.status);
  const resultWarning = ["partial", "stale", "failed", "cancelled"].includes(
    job.status,
  );
  const allTasksTerminal = job.tasks.length > 0
    && job.tasks.every((task) => TERMINAL_TASK_STATES.includes(task.status));

  return {
    stages: [
      {
        id: "snapshot",
        label: "Snapshot",
        detail: job.snapshot_available
          ? "Immutable workspace captured"
          : terminal
            ? "Snapshot released"
            : "Preparing workspace",
        state: stageState(Boolean(job.workspace_revision), !job.workspace_revision),
      },
      {
        id: "dispatch",
        label: "Dispatch",
        detail: job.tasks.length > 0
          ? `${job.tasks.length} reviewer contracts created`
          : "Waiting for reviewer contracts",
        state: stageState(
          job.tasks.length > 0 && job.status !== "queued",
          job.status === "queued",
        ),
      },
      {
        id: "parallel",
        label: "Parallel review",
        detail: `${completedTasks}/${job.tasks.length} reviewers completed`,
        state: stageState(
          allTasksTerminal,
          job.status === "running" || job.status === "cancelling",
          terminal && affectedTasks > 0,
        ),
      },
      {
        id: "aggregate",
        label: "Aggregate",
        detail: terminal
          ? `${findings.length} accepted · ${job.result.invalid_finding_count || 0} excluded`
          : job.status === "aggregating"
            ? "Validating and deduplicating evidence"
            : "Waiting for reviewer reports",
        state: stageState(terminal, job.status === "aggregating"),
      },
      {
        id: "result",
        label: "Result",
        detail: terminal
          ? job.status === "stale"
            ? "Workspace revision changed"
            : `${job.status} review`
          : "Not available yet",
        state: stageState(terminal, false, resultWarning),
      },
    ],
    completedTasks,
    activeTasks,
    affectedTasks,
    severity: {
      HIGH: findings.filter((finding) => finding.severity === "HIGH").length,
      MEDIUM: findings.filter((finding) => finding.severity === "MEDIUM").length,
      LOW: findings.filter((finding) => finding.severity === "LOW").length,
    },
    verifiedFindings: findings.filter(
      (finding) => finding.evidence_status === "verified",
    ).length,
    corroboratedFindings: findings.filter(
      (finding) => (finding.corroboration_count || 0) > 1,
    ).length,
  };
}

function formatElapsed(startedAt?: string | null, completedAt?: string | null) {
  if (!startedAt) return "Not started";
  const start = Date.parse(startedAt);
  const end = completedAt ? Date.parse(completedAt) : Date.now();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) {
    return "Timing unavailable";
  }
  const seconds = Math.max(1, Math.round((end - start) / 1000));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function attemptSummary(attempt: ReviewTaskAttempt) {
  const tokens = attempt.result.tokens_used;
  const pieces = [
    `Attempt ${attempt.attempt_number}`,
    attempt.status,
    formatElapsed(attempt.started_at, attempt.completed_at),
  ];
  if (typeof tokens === "number" && Number.isFinite(tokens)) {
    pieces.push(`${tokens.toLocaleString()} tokens`);
  }
  return pieces.join(" · ");
}

function taskTokenCount(task: ReviewTask) {
  const direct = task.result.tokens_used;
  if (typeof direct === "number" && Number.isFinite(direct)) return direct;
  return task.attempts.reduce((total, attempt) => {
    const value = attempt.result.tokens_used;
    return total + (
      typeof value === "number" && Number.isFinite(value) ? value : 0
    );
  }, 0);
}

function shortValue(value: string, length = 12) {
  return value ? value.slice(0, length) : "Unavailable";
}

interface ReviewOrchestrationPanelProps {
  job: ReviewJob;
  retryingTaskId?: string;
  lifecycleBusy?: boolean;
  onOpenSession: (sessionId: string) => void;
  onRetryTask: (taskId: string) => void;
}

export function ReviewOrchestrationPanel({
  job,
  retryingTaskId = "",
  lifecycleBusy = false,
  onOpenSession,
  onRetryTask,
}: ReviewOrchestrationPanelProps) {
  const model = useMemo(() => deriveReviewOrchestration(job), [job]);
  const [selectedTaskId, setSelectedTaskId] = useState(job.tasks[0]?.id || "");

  useEffect(() => {
    if (!job.tasks.some((task) => task.id === selectedTaskId)) {
      setSelectedTaskId(job.tasks[0]?.id || "");
    }
  }, [job.id, job.tasks, selectedTaskId]);

  const selectedTask = job.tasks.find((task) => task.id === selectedTaskId)
    || job.tasks[0];
  const canRetryTask = isTerminalJob(job.status)
    && Boolean(job.snapshot_available)
    && Boolean(selectedTask)
    && ["partial", "failed", "cancelled"].includes(selectedTask.status);

  return (
    <div className="review-orchestration">
      <ol className="review-stage-rail" aria-label="Review execution stages">
        {model.stages.map((stage, index) => (
          <li
            key={stage.id}
            className={`review-stage review-stage-${stage.state}`}
            aria-current={stage.state === "active" ? "step" : undefined}
          >
            <span className="review-stage-index">
              {stage.state === "complete" ? "✓" : index + 1}
            </span>
            <div>
              <strong>{stage.label}</strong>
              <small>{stage.detail}</small>
            </div>
          </li>
        ))}
      </ol>

      <div className="review-orchestration-grid">
        <section className="review-topology" aria-label="Reviewer topology">
          <div className="review-section-heading">
            <div>
              <span className="summary-label">Scheduling topology</span>
              <h4>Coordinator and isolated reviewers</h4>
            </div>
            <span className={`trace-pill multi-review-status ${job.status}`}>
              {job.status}
            </span>
          </div>

          <div className="review-coordinator-node">
            <span className="review-node-icon">C</span>
            <div>
              <strong>Review coordinator</strong>
              <small>
                Dispatches read-only contracts and validates returned evidence
              </small>
            </div>
            <span>
              {isActiveJob(job.status) ? "Orchestrating" : "Lifecycle complete"}
            </span>
          </div>

          <div className="review-topology-connector" aria-hidden="true" />
          <div className="review-agent-lanes">
            {job.tasks.map((task, index) => {
              const tokens = taskTokenCount(task);
              const isSelected = task.id === selectedTask?.id;
              return (
                <button
                  key={task.id}
                  type="button"
                  className={`review-agent-node ${isSelected ? "selected" : ""}`}
                  onClick={() => setSelectedTaskId(task.id)}
                  aria-pressed={isSelected}
                >
                  <span className={`review-agent-state review-agent-state-${task.status}`} />
                  <span className="review-agent-order">A{index + 1}</span>
                  <strong>{task.title}</strong>
                  <small>{task.lens.replace(/_/g, " ")}</small>
                  <span className="review-agent-metrics">
                    {task.status} · {formatElapsed(task.started_at, task.completed_at)}
                    {tokens > 0 ? ` · ${tokens.toLocaleString()} tok` : ""}
                  </span>
                </button>
              );
            })}
          </div>
        </section>

        <aside className="review-context-card">
          <div className="review-section-heading">
            <div>
              <span className="summary-label">Isolation contract</span>
              <h4>Shared facts, separate context</h4>
            </div>
          </div>
          <dl className="review-fact-list">
            <div>
              <dt>Workspace revision</dt>
              <dd title={job.workspace_revision}>{shortValue(job.workspace_revision)}</dd>
            </div>
            <div>
              <dt>Head commit</dt>
              <dd title={job.head_commit}>{shortValue(job.head_commit)}</dd>
            </div>
            <div>
              <dt>Diff fingerprint</dt>
              <dd title={job.diff_hash}>{shortValue(job.diff_hash)}</dd>
            </div>
            <div>
              <dt>Snapshot</dt>
              <dd>{job.snapshot_available ? "Retained · read only" : "Released"}</dd>
            </div>
            <div>
              <dt>Scope</dt>
              <dd>{job.changed_files.length} changed files</dd>
            </div>
            <div>
              <dt>Communication</dt>
              <dd>Reports return to coordinator only</dd>
            </div>
          </dl>
          {job.focus && (
            <div className="review-focus-note">
              <span>User focus</span>
              <p>{job.focus}</p>
            </div>
          )}
        </aside>
      </div>

      {selectedTask && (
        <section className="review-task-inspector">
          <div className="review-task-inspector-head">
            <div>
              <span className="summary-label">Selected reviewer</span>
              <h4>{selectedTask.title}</h4>
              <p>
                Independent child context · same immutable diff · one structured
                ReportFindings deliverable
              </p>
            </div>
            <div className="review-task-inspector-actions">
              {selectedTask.child_session_id && (
                <button
                  type="button"
                  className="review-session-link"
                  onClick={() => onOpenSession(selectedTask.child_session_id)}
                >
                  Open child run
                </button>
              )}
              {canRetryTask && (
                <button
                  type="button"
                  className="review-task-retry"
                  disabled={Boolean(retryingTaskId) || lifecycleBusy}
                  onClick={() => onRetryTask(selectedTask.id)}
                >
                  {retryingTaskId === selectedTask.id ? "Retrying..." : "Retry reviewer"}
                </button>
              )}
            </div>
          </div>

          <div className="review-task-detail-grid">
            <div>
              <span>Status</span>
              <strong>{selectedTask.status}</strong>
            </div>
            <div>
              <span>Elapsed</span>
              <strong>
                {formatElapsed(selectedTask.started_at, selectedTask.completed_at)}
              </strong>
            </div>
            <div>
              <span>Attempts</span>
              <strong>{selectedTask.attempts.length}</strong>
            </div>
            <div>
              <span>Tokens</span>
              <strong>{taskTokenCount(selectedTask).toLocaleString()}</strong>
            </div>
          </div>

          {selectedTask.error && (
            <div className="multi-review-error">{selectedTask.error}</div>
          )}
          {selectedTask.attempts.length > 0 && (
            <ol className="review-attempt-timeline">
              {[...selectedTask.attempts].reverse().map((attempt) => (
                <li key={attempt.id}>
                  <span className={`review-agent-state review-agent-state-${attempt.status}`} />
                  <div>
                    <strong>{attemptSummary(attempt)}</strong>
                    {attempt.error && <small>{attempt.error}</small>}
                  </div>
                  {attempt.child_session_id && (
                    <button
                      type="button"
                      onClick={() => onOpenSession(attempt.child_session_id)}
                    >
                      View run
                    </button>
                  )}
                </li>
              ))}
            </ol>
          )}
        </section>
      )}

      <section className="review-aggregation-strip">
        <div>
          <span>Accepted findings</span>
          <strong>
            {job.result.finding_count ?? job.result.findings?.length ?? 0}
          </strong>
        </div>
        <div className="severity-high">
          <span>High</span>
          <strong>{model.severity.HIGH}</strong>
        </div>
        <div className="severity-medium">
          <span>Medium</span>
          <strong>{model.severity.MEDIUM}</strong>
        </div>
        <div>
          <span>Evidence verified</span>
          <strong>{model.verifiedFindings}</strong>
        </div>
        <div>
          <span>Corroborated</span>
          <strong>{model.corroboratedFindings}</strong>
        </div>
        <div>
          <span>Excluded</span>
          <strong>{job.result.invalid_finding_count || 0}</strong>
        </div>
      </section>
    </div>
  );
}
