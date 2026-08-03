import { useEffect, useState } from "react";
import type { PlanEntry, PlanDetail, PlanRevision, PlanRevisionDiff } from "../api/plans";
import * as plansApi from "../api/plans";
import { useSessionStore } from "../stores/sessionStore";
import { MarkdownRenderer } from "./MarkdownRenderer";
import type { ViewName } from "../navigation";

type LibraryState =
  | { phase: "loading" }
  | { phase: "error"; message: string; retry?: () => void }
  | { phase: "ready" };

export function PlanLibrary({ onNavigate }: { onNavigate: (view: ViewName) => void }) {
  const [ui, setUi] = useState<LibraryState>({ phase: "loading" });
  const [plans, setPlans] = useState<PlanEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [detail, setDetail] = useState<PlanDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // ── Revisions (per selected plan's session) ──
  const [revisions, setRevisions] = useState<PlanRevision[]>([]);
  const [revisionsLoading, setRevisionsLoading] = useState(false);
  const [showingRevision, setShowingRevision] = useState<number | null>(null);
  const [revisionContent, setRevisionContent] = useState<string | null>(null);

  // ── Diff ──
  const [diffTarget, setDiffTarget] = useState<number | null>(null);
  const [diffResult, setDiffResult] = useState<PlanRevisionDiff | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);

  const load = () => {
    setUi({ phase: "loading" });
    plansApi.listPlans().then(
      (res) => { setPlans(res.plans); setTotal(res.total); setUi({ phase: "ready" }); },
      (err) => setUi({ phase: "error", message: err instanceof Error ? err.message : "Failed to load", retry: load }),
    );
  };

  useEffect(() => { load(); }, []);

  const selectPlan = async (plan: PlanEntry) => {
    setSelectedFile(plan.filename);
    setDetail(null);
    setRevisions([]);
    setShowingRevision(null);
    setRevisionContent(null);
    setDiffTarget(null);
    setDiffResult(null);
    setDetailLoading(true);
    try {
      const d = await plansApi.getPlan(plan.filename);
      setDetail(d);
    } catch {
      setDetail(null);
    } finally {
      setDetailLoading(false);
    }
    // Load revisions if session is linked
    if (plan.session?.id) {
      setRevisionsLoading(true);
      try {
        const revs = await plansApi.listPlanRevisions(plan.session.id);
        setRevisions(revs);
      } catch {
        setRevisions([]);
      } finally {
        setRevisionsLoading(false);
      }
    }
  };

  const handleDelete = async (plan: PlanEntry) => {
    if (!window.confirm(`Delete "${plan.title}"? This is permanent.`)) return;
    try {
      await plansApi.deletePlan(plan.filename);
      setPlans((prev) => prev.filter((p) => p.filename !== plan.filename));
      setTotal((t) => t - 1);
      if (selectedFile === plan.filename) {
        setSelectedFile(null);
        setDetail(null);
      }
    } catch (err) {
      alert(err instanceof Error ? err.message : "Delete failed");
    }
  };

  const loadRevision = async (rev: number) => {
    if (!detail?.session_id) return;
    if (rev === showingRevision) { setShowingRevision(null); setRevisionContent(null); return; }
    setShowingRevision(rev);
    setDiffTarget(null);
    setDiffResult(null);
    setRevisionContent(null);
    try {
      const r = await plansApi.getPlanRevision(detail.session_id, rev);
      setRevisionContent(r.content);
    } catch {
      setRevisionContent("(failed to load)");
    }
  };

  const loadDiff = async (fromRev: number, toRev: number) => {
    if (!detail?.session_id) return;
    setDiffTarget(toRev);
    setDiffResult(null);
    setDiffLoading(true);
    try {
      const diff = await plansApi.diffPlanRevisions(detail.session_id, fromRev, toRev);
      setDiffResult(diff);
    } catch {
      setDiffResult(null);
    } finally {
      setDiffLoading(false);
    }
  };

  const formatDate = (iso: string) => {
    try { return new Date(iso).toLocaleString(); } catch { return iso; }
  };

  if (ui.phase === "loading") {
    return <div className="plan-library-skeleton">{Array.from({ length: 6 }, (_, i) => <div key={i} className="skeleton-line" style={{ width: `${60 + i * 10}%` }} />)}</div>;
  }

  if (ui.phase === "error") {
    return (
      <div className="plan-library-error">
        <div>{ui.message}</div>
        {ui.retry && <button className="btn-ghost" onClick={ui.retry}>Retry</button>}
      </div>
    );
  }

  return (
    <div className="plan-library">
      <div className="plan-library-header">
        <div>
          <div className="memory-hero-title" style={{ fontSize: 24, margin: 0 }}>Plan Library</div>
          <div className="review-card-meta" style={{ marginTop: 4 }}>
            {total} plan{total !== 1 ? "s" : ""} from past sessions
          </div>
        </div>
      </div>

      <div className="plan-library-layout">
        {/* ── Left: catalog ── */}
        <div className="plan-library-catalog">
          {plans.length === 0 ? (
            <div className="plan-library-empty">No plans yet. Use <code>/plan</code> in Chat to create one.</div>
          ) : (
            plans.map((plan) => (
              <button
                key={plan.filename}
                type="button"
                className={`plan-library-item ${selectedFile === plan.filename ? "active" : ""}`}
                onClick={() => selectPlan(plan)}
              >
                <div className="plan-library-item-top">
                  <span className="plan-library-item-title">{plan.title}</span>
                  {plan.session?.status && (
                    <span className={`session-status-pill status-${plan.session.status}`}>
                      <span className="plan-library-item-dot" />{plan.session.status}
                    </span>
                  )}
                </div>
                <div className="plan-library-item-preview">{plan.preview}</div>
                <div className="plan-library-item-meta">
                  <span>{plan.size_bytes.toLocaleString()} bytes</span>
                  <span>{formatDate(plan.created_at)}</span>
                  {plan.session?.agent_name && <span>{plan.session.agent_name}</span>}
                </div>
              </button>
            ))
          )}
        </div>

        {/* ── Right: detail ── */}
        <div className="plan-library-detail">
          {!selectedFile ? (
            <div className="plan-library-empty">Select a plan from the list to view its content.</div>
          ) : detailLoading ? (
            <div className="plan-library-skeleton">
              <div className="skeleton-line" style={{ width: "80%" }} />
              <div className="skeleton-line" style={{ width: "60%" }} />
              <div className="skeleton-line" style={{ width: "70%" }} />
            </div>
          ) : detail ? (
            <>
              <div className="plan-library-detail-header">
                <div>
                  <div className="plan-library-detail-title">{detail.title}</div>
                  <div className="plan-library-detail-meta">
                    <span>{detail.filename}</span>
                    <span>{detail.size_bytes.toLocaleString()} bytes</span>
                    {detail.session && <span>Agent: {detail.session.agent_name}</span>}
                  </div>
                </div>
                <div className="plan-library-detail-actions">
                  <button
                    className="btn-primary"
                    type="button"
                    onClick={async () => {
                      const sessionId = await useSessionStore.getState().createSession(
                        detail.session?.agent_name || "build",
                        ".",
                        detail.filename,
                      );
                      if (sessionId) onNavigate("chat");
                    }}
                    title="Open a fresh session seeded with this plan"
                  >
                    Continue in Chat →
                  </button>
                  <button className="btn-ghost" onClick={() => detail && handleDelete(detail)} title="Delete this plan">
                    Delete
                  </button>
                </div>
              </div>

              {/* ── Revisions ── */}
              {detail.session_id && revisions.length > 0 && (
                <div className="plan-library-revisions">
                  <div className="plan-goals-title">Revision history ({revisions.length})</div>
                  <div className="plan-library-revision-list">
                    {revisions.map((rev) => (
                      <div key={rev.revision} className={`plan-library-revision-row ${showingRevision === rev.revision ? "active" : ""}`}>
                        <button type="button" className="plan-chrono-btn" onClick={() => loadRevision(rev.revision)}>
                          v{rev.revision}
                          <span className="plan-library-item-meta" style={{ marginLeft: 8 }}>
                            {rev.status} &middot; {formatDate(rev.created_at)}
                          </span>
                          {rev.change_request && (
                            <span className="plan-library-item-meta" style={{ marginLeft: 8, fontStyle: "italic" }} title={rev.change_request}>
                              "{rev.change_request.slice(0, 80)}{rev.change_request.length > 80 ? "…" : ""}"
                            </span>
                          )}
                        </button>
                        {revisions.length > 0 && showingRevision !== rev.revision && (
                          <button
                            type="button"
                            className="plan-diff-btn"
                            onClick={() => {
                              const prev = revisions.filter((r) => r.revision < rev.revision).sort((a, b) => b.revision - a.revision)[0];
                              if (prev) loadDiff(prev.revision, rev.revision);
                            }}
                            title="Compare with previous revision"
                          >
                            Diff
                          </button>
                        )}
                      </div>
                    ))}
                  </div>
                  {revisionContent !== null && (
                    <div className="plan-scroll" style={{ marginTop: 12 }}>
                      <pre className="plan-pre"><MarkdownRenderer content={revisionContent} /></pre>
                    </div>
                  )}
                </div>
              )}

              {/* ── Diff view ── */}
              {diffResult && (
                <div className="plan-library-section" style={{ marginTop: 16 }}>
                  <div className="plan-goals-title">
                    Diff: v{diffResult.from_revision} → v{diffResult.to_revision}
                    <button type="button" className="plan-diff-close" onClick={() => { setDiffResult(null); setDiffTarget(null); }} style={{ marginLeft: 10, background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)" }}>
                      ✕
                    </button>
                  </div>
                  <div className="diff-block">
                    {(diffResult.diff || "").split("\n").map((line, i) => {
                      let cls = "diff-line-meta";
                      if (line.startsWith("+") && !line.startsWith("+++")) cls = "diff-line-added";
                      else if (line.startsWith("-") && !line.startsWith("---")) cls = "diff-line-removed";
                      else if (line.startsWith("@@")) cls = "diff-line-hunk";
                      return (
                        <div key={i} className={`diff-line ${cls}`}>
                          <span className="diff-line-gutter">{i + 1}</span>
                          <span className="diff-line-code">{line}</span>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
              {diffLoading && <div className="plan-library-skeleton" style={{ marginTop: 12 }}><div className="skeleton-line" /></div>}

              {/* ── Full content ── */}
              <div className="plan-library-section" style={{ marginTop: 16 }}>
                <div className="plan-goals-title">Content</div>
                <div className="plan-scroll">
                  <pre className="plan-pre"><MarkdownRenderer content={detail.content} /></pre>
                </div>
              </div>
            </>
          ) : !detailLoading ? (
            <div className="plan-library-empty">Failed to load plan.</div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
