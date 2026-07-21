import { useEffect, useMemo, useState } from "react";
import { getPendingDiffs, updateDiffStatus } from "../api/diffs";
import { DiffBlock } from "./DiffBlock";
import { useSessionStore } from "../stores/sessionStore";
import type { SessionDiff } from "../types/stats";

function EmptyState() {
  return (
    <div className="plan-empty">
      <div className="plan-empty-icon">R</div>
      <div className="plan-empty-title">No pending reviews</div>
      <div className="plan-empty-body">
        Pending file diffs will appear here once the agent produces editable changes that need review.
      </div>
    </div>
  );
}

export function DiffReviewView() {
  const [diffs, setDiffs] = useState<SessionDiff[]>([]);
  const [loading, setLoading] = useState(true);
  const [submittingId, setSubmittingId] = useState<number | null>(null);
  const [submittingAny, setSubmittingAny] = useState(false);
  const [comments, setComments] = useState<Record<number, string>>({});

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getPendingDiffs()
      .then((items) => {
        if (!cancelled) setDiffs(items);
      })
      .catch(() => {
        if (!cancelled) setDiffs([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const pendingCount = diffs.filter((item) => item.status === "pending").length;

  const sortedDiffs = useMemo(
    () =>
      [...diffs].sort((a, b) => {
        if (a.status !== b.status) return a.status === "pending" ? -1 : 1;
        return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
      }),
    [diffs],
  );

  const handleDecision = async (diff: SessionDiff, status: "approved" | "rejected") => {
    if (submittingAny) return;
    setSubmittingAny(true);
    setSubmittingId(diff.id);
    try {
      await updateDiffStatus(diff.id, status, comments[diff.id] || "");
      setDiffs((prev) => prev.filter((item) => item.id !== diff.id));
    } finally {
      setSubmittingAny(false);
      setSubmittingId(null);
    }
  };

  return (
    <section className="view active" data-view-name="reviews">
      <div className="plan-page review-page">
        <div className="plan-hero review-hero">
          <div>
            <div className="summary-label">Review Workspace</div>
            <h2 className="plan-hero-title">Review pending diffs before they land</h2>
            <p className="plan-hero-body">
              This queue collects code changes that still need human review. Approve, reject, and leave comments with the diff in view.
            </p>
          </div>
          <div className="plan-hero-stats">
            <div className="meta-pill">
              <div className="meta-pill-label">Pending</div>
              <div className="meta-pill-value">{pendingCount}</div>
            </div>
            <div className="meta-pill">
              <div className="meta-pill-label">Loaded</div>
              <div className="meta-pill-value">{loading ? "…" : diffs.length}</div>
            </div>
          </div>
        </div>

        {loading ? (
          <div className="review-loading-card">Loading pending reviews...</div>
        ) : sortedDiffs.length === 0 ? (
          <EmptyState />
        ) : (
          <div className="review-list">
            {sortedDiffs.map((diff) => (
              <div key={diff.id} className="review-card">
                <div className="review-card-header">
                  <div>
                    <div className="summary-label">File review</div>
                    <h3 className="review-card-title">{diff.file_path}</h3>
                    <div className="review-card-meta">
                      <button
                        className="review-session-link"
                        type="button"
                        onClick={() => useSessionStore.getState().openSession(diff.session_id)}
                        title="Open session in Chat view"
                      >
                        {diff.session_title || diff.session_id}
                      </button>
                      <span>Step {diff.step_number}</span>
                      <span>{diff.session_agent || "agent"}</span>
                    </div>
                  </div>
                  <div className="review-card-actions">
                    <button
                      className="btn-approve"
                      type="button"
                      disabled={submittingId === diff.id}
                      onClick={() => handleDecision(diff, "approved")}
                    >
                      Approve
                    </button>
                    <button
                      className="btn-reject"
                      type="button"
                      disabled={submittingId === diff.id}
                      onClick={() => handleDecision(diff, "rejected")}
                    >
                      Reject
                    </button>
                  </div>
                </div>

                <DiffBlock diff={diff.diff_content} />

                <div className="review-comment-row">
                  <input
                    className="review-comment-input"
                    type="text"
                    placeholder="Leave an optional review comment..."
                    value={comments[diff.id] || ""}
                    onChange={(e) =>
                      setComments((prev) => ({
                        ...prev,
                        [diff.id]: e.target.value,
                      }))
                    }
                  />
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
