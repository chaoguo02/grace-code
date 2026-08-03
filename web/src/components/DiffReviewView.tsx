import { useEffect, useMemo, useState } from "react";
import { getSessionDiffs, getPendingDiffs, updateDiffStatus } from "../api/diffs";
import { getSessionSteps } from "../api/stats";
import {
  cancelReview,
  getLatestReview,
  getReview,
  releaseReviewSnapshot,
  retryReview,
  retryReviewTask,
  startMultiAgentReview,
} from "../api/reviews";
import { DiffBlock } from "./DiffBlock";
import { ReviewOrchestrationPanel } from "./ReviewOrchestrationPanel";
import { useSessionStore } from "../stores/sessionStore";
import { selectSessionUi, useChatStore } from "../stores/chatStore";
import type { SessionDiff, StepLog } from "../types/stats";
import type { ReviewJob } from "../api/reviews";

function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <div className="plan-empty">
      <div className="plan-empty-icon">R</div>
      <div className="plan-empty-title">{title}</div>
      <div className="plan-empty-body">{body}</div>
    </div>
  );
}

function countDiffLines(diff: string): { added: number; removed: number; total: number } {
  const lines = diff.split("\n");
  let added = 0;
  let removed = 0;
  for (const line of lines) {
    if (line.startsWith("+") && !line.startsWith("+++")) added++;
    else if (line.startsWith("-") && !line.startsWith("---")) removed++;
  }
  return { added, removed, total: lines.length };
}

function formatDuration(ms?: number) {
  if (!ms || ms <= 0) return "—";
  const sec = Math.round(ms / 1000);
  if (sec < 60) return `${sec}s`;
  return `${Math.floor(sec / 60)}m ${sec % 60}s`;
}

function collectVerificationSignals(steps: StepLog[]) {
  const signals = steps.filter((step) => {
    const tool = (step.tool_name || "").toLowerCase();
    const params = (step.tool_params || "").toLowerCase();
    return tool.includes("bash")
      || tool.includes("powershell")
      || params.includes("test")
      || params.includes("build")
      || params.includes("lint")
      || params.includes("typecheck")
      || params.includes("tsc")
      || params.includes("playwright");
  });
  return signals.slice(-6).reverse();
}

export function DiffReviewView() {
  const activeId = useSessionStore((s) => s.activeId);
  const activeDetail = useSessionStore((s) => s.activeDetail);
  const reviewEventKey = useChatStore((state) => {
    if (!activeId) return "";
    const event = selectSessionUi(state, activeId).events.find(
      (candidate) => candidate.type === "review_updated",
    );
    return event?.type === "review_updated"
      ? `${event.job_id}:${event.status}`
      : "";
  });

  const [globalDiffs, setGlobalDiffs] = useState<SessionDiff[]>([]);
  const [sessionDiffs, setSessionDiffs] = useState<SessionDiff[]>([]);
  const [steps, setSteps] = useState<StepLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [submittingId, setSubmittingId] = useState<number | null>(null);
  const [submittingAny, setSubmittingAny] = useState(false);
  const [comments, setComments] = useState<Record<number, string>>({});
  const [expandedDiffs, setExpandedDiffs] = useState<Set<number>>(new Set());
  const [errors, setErrors] = useState<Record<number, string>>({});
  const [reviewJob, setReviewJob] = useState<ReviewJob | null>(null);
  const [reviewFocus, setReviewFocus] = useState("");
  const [reviewStarting, setReviewStarting] = useState(false);
  const [reviewTaskRetrying, setReviewTaskRetrying] = useState("");
  const [reviewError, setReviewError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setReviewJob(null);
    setReviewError("");
    Promise.all([
      getPendingDiffs().catch(() => []),
      activeId ? getSessionDiffs(activeId).catch(() => []) : Promise.resolve([]),
      activeId ? getSessionSteps(activeId).catch(() => []) : Promise.resolve([]),
      activeId ? getLatestReview(activeId).catch(() => null) : Promise.resolve(null),
    ]).then(([pendingData, sessionDiffData, stepsData, latestReview]) => {
      if (cancelled) return;
      setGlobalDiffs(pendingData as SessionDiff[]);
      setSessionDiffs(sessionDiffData as SessionDiff[]);
      setSteps(stepsData as StepLog[]);
      setReviewJob(latestReview as ReviewJob | null);
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [activeId]);

  useEffect(() => {
    if (
      !activeId
      || !reviewJob
      || reviewJob.session_id !== activeId
      || !["queued", "running", "aggregating", "cancelling"].includes(reviewJob.status)
    ) {
      return;
    }
    const timer = window.setInterval(() => {
      getReview(reviewJob.id)
        .then(setReviewJob)
        .catch((error) => {
          setReviewError(
            error instanceof Error ? error.message : "Review status refresh failed",
          );
        });
    }, 1500);
    return () => window.clearInterval(timer);
  }, [activeId, reviewJob?.id, reviewJob?.session_id, reviewJob?.status]);

  useEffect(() => {
    const jobId = reviewEventKey.split(":", 1)[0];
    if (!jobId || !activeId) return;
    getReview(jobId)
      .then((job) => {
        if (job.session_id === activeId) setReviewJob(job);
      })
      .catch(() => {});
  }, [activeId, reviewEventKey]);

  const handleStartReview = async () => {
    if (!activeId || reviewStarting) return;
    setReviewStarting(true);
    setReviewError("");
    try {
      setReviewJob(
        await startMultiAgentReview(activeId, reviewFocus.trim(), 3),
      );
    } catch (error) {
      setReviewError(
        error instanceof Error ? error.message : "Unable to start review",
      );
    } finally {
      setReviewStarting(false);
    }
  };

  const handleReviewLifecycle = async (
    action: "cancel" | "retry" | "release",
  ) => {
    if (!reviewJob || reviewStarting) return;
    setReviewStarting(true);
    setReviewError("");
    try {
      setReviewJob(
        action === "cancel"
          ? await cancelReview(reviewJob.id)
          : action === "retry"
            ? await retryReview(reviewJob.id)
            : await releaseReviewSnapshot(reviewJob.id),
      );
    } catch (error) {
      setReviewError(
        error instanceof Error ? error.message : `Unable to ${action} review`,
      );
    } finally {
      setReviewStarting(false);
    }
  };

  const handleTaskRetry = async (taskId: string) => {
    if (!reviewJob || reviewStarting || reviewTaskRetrying) return;
    setReviewTaskRetrying(taskId);
    setReviewError("");
    try {
      setReviewJob(await retryReviewTask(reviewJob.id, taskId));
    } catch (error) {
      setReviewError(
        error instanceof Error ? error.message : "Unable to retry reviewer",
      );
    } finally {
      setReviewTaskRetrying("");
    }
  };

  const pendingQueue = useMemo(
    () => globalDiffs
      .filter((item) => item.status === "pending")
      .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()),
    [globalDiffs],
  );

  const sessionPending = sessionDiffs.filter((item) => item.status === "pending").length;

  const verificationSignals = useMemo(() => collectVerificationSignals(steps), [steps]);
  const failedSignals = verificationSignals.filter((step) => step.status === "error" || step.status === "failed").length;
  const readiness = !activeId
    ? "Open a session to review its outcome"
    : loading
      ? "Loading review signals"
      : failedSignals > 0
        ? "Needs attention"
        : sessionPending > 0
          ? "Pending decisions"
          : activeDetail?.status === "completed"
            ? "Ready for handoff"
            : activeDetail?.status === "running"
              ? "Run in progress"
              : "Review available";

  const handleDecision = async (diff: SessionDiff, status: "approved" | "rejected") => {
    if (submittingAny) return;
    setSubmittingAny(true);
    setSubmittingId(diff.id);
    setErrors((prev) => { const next = { ...prev }; delete next[diff.id]; return next; });
    try {
      await updateDiffStatus(diff.id, status, comments[diff.id] || "");
      setGlobalDiffs((prev) => prev.filter((item) => item.id !== diff.id));
      setSessionDiffs((prev) => prev.map((item) => item.id === diff.id ? { ...item, status } : item));
    } catch {
      setErrors((prev) => ({ ...prev, [diff.id]: `Failed to ${status} diff — try again` }));
    } finally {
      setSubmittingAny(false);
      setSubmittingId(null);
    }
  };

  const toggleExpand = (id: number) => {
    setExpandedDiffs((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <section className="view active" data-view-name="reviews">
      <div className="plan-page review-page">
        <div className="plan-hero review-hero">
          <div>
            <div className="summary-label">Changes</div>
            <h2 className="plan-hero-title">Review what changed, then hand it off</h2>
            <p className="plan-hero-body">
              Resolve pending decisions first, then confirm verification and supporting review evidence before handoff.
            </p>
          </div>
          <div className="plan-hero-stats">
            <div className="meta-pill">
              <div className="meta-pill-label">Readiness</div>
              <div className="meta-pill-value">{readiness}</div>
            </div>
            <div className="meta-pill">
              <div className="meta-pill-label">Pending</div>
              <div className="meta-pill-value">{pendingQueue.length}</div>
            </div>
          </div>
        </div>

        <div className="stats-card stats-card-wide multi-review-panel">
          <div className="stats-card-header multi-review-header">
            <div>
              <div className="summary-label">Multi-agent review</div>
              <h3 className="stats-card-title">
                Independent review lenses, one verified result
              </h3>
            </div>
            <div className="multi-review-actions">
              <input
                className="review-comment-input multi-review-focus"
                value={reviewFocus}
                onChange={(event) => setReviewFocus(event.target.value)}
                placeholder="Optional review focus"
                disabled={!activeId || reviewStarting}
              />
              <button
                className="btn-approve"
                type="button"
                disabled={
                  !activeId
                  || reviewStarting
                  || Boolean(
                    reviewJob
                    && ["queued", "running", "aggregating", "cancelling"].includes(reviewJob.status),
                  )
                }
                onClick={handleStartReview}
              >
                {reviewStarting ? "Starting..." : "Run review"}
              </button>
              {reviewJob && ["queued", "running", "aggregating", "cancelling"].includes(reviewJob.status) && (
                <button
                  className="btn-reject multi-review-lifecycle"
                  type="button"
                  disabled={reviewStarting || reviewJob.status === "cancelling"}
                  onClick={() => handleReviewLifecycle("cancel")}
                >
                  {reviewJob.status === "cancelling" ? "Cancelling..." : "Cancel"}
                </button>
              )}
              {reviewJob && ["completed", "partial", "stale", "failed", "cancelled"].includes(reviewJob.status) && (
                <>
                  <button
                    className="multi-review-lifecycle multi-review-retry"
                    type="button"
                    disabled={reviewStarting}
                    onClick={() => handleReviewLifecycle("retry")}
                  >
                    Retry current code
                  </button>
                  {reviewJob.snapshot_available && (
                    <button
                      className="multi-review-lifecycle multi-review-release"
                      type="button"
                      disabled={reviewStarting}
                      onClick={() => handleReviewLifecycle("release")}
                    >
                      Release snapshot
                    </button>
                  )}
                </>
              )}
            </div>
          </div>

          {reviewError && <div className="multi-review-error">{reviewError}</div>}
          {!reviewJob ? (
            <EmptyState
              title="No multi-agent review yet"
              body="Run three read-only reviewers against one immutable workspace snapshot."
            />
          ) : (
            <>
              <div className="multi-review-meta">
                <span className={`trace-pill multi-review-status ${reviewJob.status}`}>
                  {reviewJob.status}
                </span>
                <span>Revision {reviewJob.workspace_revision.slice(0, 10)}</span>
                <span>
                  {reviewJob.snapshot_available
                    ? "Frozen snapshot retained"
                    : "Snapshot released"}
                </span>
                <span>{reviewJob.changed_files.length} files</span>
                <span>{reviewJob.result.total_tokens?.toLocaleString() || 0} tokens</span>
              </div>

              <ReviewOrchestrationPanel
                job={reviewJob}
                retryingTaskId={reviewTaskRetrying}
                lifecycleBusy={reviewStarting}
                onOpenSession={(sessionId) => (
                  useSessionStore.getState().openSession(sessionId)
                )}
                onRetryTask={handleTaskRetry}
              />

              {reviewJob.status === "stale" && (
                <div className="multi-review-stale">
                  The workspace changed during review. These findings are not
                  authoritative for the current code; run the review again.
                </div>
              )}
              {reviewJob.error && (
                <div className="multi-review-error">{reviewJob.error}</div>
              )}
              {(reviewJob.result.invalid_finding_count || 0) > 0 && (
                <div className="multi-review-invalid">
                  {reviewJob.result.invalid_finding_count} reviewer finding
                  {reviewJob.result.invalid_finding_count === 1 ? " was" : "s were"} excluded:
                  its file, line, snippet, or verification evidence did not match
                  the frozen snapshot.
                </div>
              )}

              {(reviewJob.result.findings || []).length > 0 ? (
                <div className="multi-review-findings">
                  {(reviewJob.result.findings || []).map((finding, index) => (
                    <article
                      key={`${finding.file_path || "general"}-${finding.line_start || 0}-${index}`}
                      className={`multi-review-finding severity-${finding.severity.toLowerCase()}`}
                    >
                      <div className="multi-review-finding-header">
                        <span>{finding.severity}</span>
                        <strong>{finding.title}</strong>
                        {(finding.corroboration_count || 0) > 1 && (
                          <span className="trace-pill">
                            {finding.corroboration_count} reviewers
                          </span>
                        )}
                        {finding.evidence_status && (
                          <span className={`multi-review-evidence ${finding.evidence_status}`}>
                            {finding.evidence_status === "verified"
                              ? "Evidence verified"
                              : "Hypothesis"}
                          </span>
                        )}
                      </div>
                      <div className="multi-review-location">
                        {finding.file_path || "General"}
                        {finding.line_start ? `:${finding.line_start}` : ""}
                      </div>
                      <p>{finding.description}</p>
                      {finding.verification && (
                        <small>Verified: {finding.verification}</small>
                      )}
                    </article>
                  ))}
                </div>
              ) : ["completed", "partial"].includes(reviewJob.status) ? (
                <div className="multi-review-clean">
                  No evidence-backed findings were reported for this snapshot.
                </div>
              ) : null}
            </>
          )}
        </div>

        <details className="stats-card stats-card-wide review-verification">
          <summary className="stats-card-header">
            <div>
              <div className="summary-label">Verification</div>
              <h3 className="stats-card-title">Build, test, and command signals</h3>
            </div>
          </summary>
          {loading ? (
            <div className="empty-state">Loading verification signals...</div>
          ) : verificationSignals.length === 0 ? (
            <EmptyState title="No verification signals yet" body="Build, test, lint, or typecheck commands will appear here when this session records them." />
          ) : (
            <div className="stats-session-list">
              {verificationSignals.map((step) => (
                <div key={step.id} className="stats-session-row">
                  <div className="stats-session-main">
                    <strong>{step.tool_name}</strong>
                    <span>{step.status}</span>
                    <span>Step {step.step_number}</span>
                    <span>{formatDuration(step.duration_ms)}</span>
                    <span>{step.tokens ? `${step.tokens.toLocaleString()} tok` : "—"}</span>
                  </div>
                  <div className="stats-session-subtle">{step.tool_params}</div>
                </div>
              ))}
            </div>
          )}
        </details>

        <div className="stats-card stats-card-wide">
          <div className="stats-card-header">
            <div>
              <div className="summary-label">Pending change decisions</div>
              <h3 className="stats-card-title">Proposed file changes that still need action</h3>
            </div>
          </div>

          {loading ? (
            <div className="review-loading-card">Loading pending decisions...</div>
          ) : pendingQueue.length === 0 ? (
            <EmptyState title="No pending change decisions" body="Proposed file changes will appear here only when they still require an explicit approve or reject decision." />
          ) : (
            <div className="review-list">
              {pendingQueue.map((diff) => {
                const lineStats = countDiffLines(diff.diff_content);
                const isExpanded = expandedDiffs.has(diff.id);
                return (
                  <div key={diff.id} className="review-card">
                    <div className="review-card-header">
                      <div>
                        <div className="review-card-file-row">
                          <span className="review-card-file-icon">F</span>
                          <h3 className="review-card-title">{diff.file_path}</h3>
                        </div>
                        <div className="review-card-meta">
                          <button
                            className="review-session-link"
                            type="button"
                            onClick={() => useSessionStore.getState().openSession(diff.session_id)}
                            title="Open session in Chat view"
                          >
                            {diff.session_title || diff.session_id.slice(0, 8)}
                          </button>
                          <span>Step {diff.step_number}</span>
                          <span>{diff.session_agent || "agent"}</span>
                          <span className="review-diff-summary">+{lineStats.added} / −{lineStats.removed}</span>
                        </div>
                      </div>
                      <div className="review-card-actions">
                        <button className="btn-approve" type="button" disabled={submittingId === diff.id} onClick={() => handleDecision(diff, "approved")}>Approve</button>
                        <button className="btn-reject" type="button" disabled={submittingId === diff.id} onClick={() => handleDecision(diff, "rejected")}>Reject</button>
                      </div>
                    </div>

                    {errors[diff.id] && (
                      <div style={{ marginTop: 8, padding: "6px 12px", borderRadius: 8, background: "var(--red, #f44336)", color: "#fff", fontSize: 12 }}>
                        {errors[diff.id]}
                      </div>
                    )}

                    <button type="button" className="review-diff-toggle" onClick={() => toggleExpand(diff.id)}>
                      <span>{isExpanded ? "▼ Hide diff" : `▶ Show diff (${lineStats.total} lines, +${lineStats.added}/−${lineStats.removed})`}</span>
                    </button>

                    {isExpanded && <DiffBlock diff={diff.diff_content} />}

                    <div className="review-comment-row">
                      <input
                        className="review-comment-input"
                        type="text"
                        placeholder="Leave an optional review comment..."
                        value={comments[diff.id] || ""}
                        onChange={(e) => setComments((prev) => ({ ...prev, [diff.id]: e.target.value }))}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
