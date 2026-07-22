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

export function DiffReviewView() {
  const [diffs, setDiffs] = useState<SessionDiff[]>([]);
  const [loading, setLoading] = useState(true);
  const [submittingId, setSubmittingId] = useState<number | null>(null);
  const [submittingAny, setSubmittingAny] = useState(false);
  const [comments, setComments] = useState<Record<number, string>>({});
  const [expandedDiffs, setExpandedDiffs] = useState<Set<number>>(new Set());
  const [errors, setErrors] = useState<Record<number, string>>({});

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

  // Precompute line stats once for all diffs (avoid O(n*m) per render)
  const diffLineStats = useMemo(() => {
    const stats = new Map<number, { added: number; removed: number; total: number }>();
    for (const diff of sortedDiffs) {
      stats.set(diff.id, countDiffLines(diff.diff_content));
    }
    return stats;
  }, [sortedDiffs]);

  const handleDecision = async (diff: SessionDiff, status: "approved" | "rejected") => {
    if (submittingAny) return;
    setSubmittingAny(true);
    setSubmittingId(diff.id);
    // Clear stale error for this diff
    setErrors((prev) => { const next = { ...prev }; delete next[diff.id]; return next; });
    try {
      await updateDiffStatus(diff.id, status, comments[diff.id] || "");
      setDiffs((prev) => prev.filter((item) => item.id !== diff.id));
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
            {sortedDiffs.map((diff) => {
              const lineStats = diffLineStats.get(diff.id) || { added: 0, removed: 0, total: 0 };
              const isExpanded = expandedDiffs.has(diff.id);
              return (
                <div key={diff.id} className="review-card">
                  {/* Header row */}
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
                        <span className="review-diff-summary">
                          +{lineStats.added} / −{lineStats.removed}
                        </span>
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

                  {/* Inline error feedback */}
                  {errors[diff.id] && (
                    <div style={{ marginTop: 8, padding: "6px 12px", borderRadius: 8, background: "var(--red, #f44336)", color: "#fff", fontSize: 12 }}>
                      {errors[diff.id]}
                    </div>
                  )}

                  {/* Diff preview / expand toggle */}
                  <button type="button" className="review-diff-toggle" onClick={() => toggleExpand(diff.id)}>
                    <span>
                      {isExpanded ? "▼ Hide diff" : `▶ Show diff (${lineStats.total} lines, +${lineStats.added}/−${lineStats.removed})`}
                    </span>
                  </button>

                  {isExpanded && <DiffBlock diff={diff.diff_content} />}

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
              );
            })}
          </div>
        )}
      </div>
    </section>
  );
}
