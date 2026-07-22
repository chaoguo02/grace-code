import { useEffect, useMemo, useState } from "react";
import { listPlans, getPlan, updatePlan, deletePlan, type PlanEntry, type PlanDetail } from "../api/plans";
import { useSessionStore } from "../stores/sessionStore";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { ConfirmModal } from "./ConfirmModal";

function formatDate(ts: string) {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleString();
}

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

function formatRelative(ts: string) {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  const diffMs = Date.now() - d.getTime();
  const diffMin = Math.round(diffMs / 60000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffH = Math.round(diffMin / 60);
  if (diffH < 24) return `${diffH}h ago`;
  return `${Math.round(diffH / 24)}d ago`;
}

export function PlansView() {
  const [plans, setPlans] = useState<PlanEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<PlanDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState("");
  const [saving, setSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const { openSession, activeId } = useSessionStore();

  const showToast = (msg: string) => { setToast(msg); setTimeout(() => setToast(null), 3000); };

  const load = () => {
    setLoading(true);
    setError(null);
    listPlans()
      .then((data) => {
        setPlans(data.plans);
        setTotal(data.total);
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "Failed to load plans"))
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, [activeId]);

  const openPlan = (entry: PlanEntry) => {
    setDetailLoading(true);
    setEditing(false);
    getPlan(entry.filename)
      .then((detail) => setSelected(detail))
      .catch(() => showToast("Failed to load plan detail"))
      .finally(() => setDetailLoading(false));
  };

  const sorted = useMemo(
    () => [...plans].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()),
    [plans],
  );

  return (
    <section className="view active" data-view-name="plans">
      <div className="plan-page">
        {/* Hero */}
        <div className="plan-hero">
          <div>
            <div className="summary-label">Plan Library</div>
            <h2 className="plan-hero-title">All generated plans</h2>
            <p className="plan-hero-body">
              Browse every plan that has been produced across all sessions. Click to inspect,
              or jump directly to the originating session.
            </p>
          </div>
          <div className="plan-hero-stats">
            <div className="meta-pill">
              <div className="meta-pill-label">Total</div>
              <div className="meta-pill-value">{total}</div>
            </div>
          </div>
        </div>

        {error && (
          <div style={{ padding: 12, background: "var(--error)", color: "#fff", borderRadius: 6, margin: "0 20px 12px", display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ fontSize: 13 }}>{error}</span>
            <button onClick={load} style={{ marginLeft: "auto", fontSize: 12, background: "rgba(255,255,255,0.2)", border: "none", borderRadius: 3, color: "#fff", cursor: "pointer", padding: "4px 10px" }}>Retry</button>
          </div>
        )}

        <div className="plans-layout">
          {/* Catalog */}
          <div className="plans-catalog">
            {loading && (
              <div style={{ padding: 20, textAlign: "center", color: "var(--text-muted)" }}>Loading plans…</div>
            )}
            {!loading && sorted.length === 0 && (
              <div className="plan-empty">
                <div className="plan-empty-icon">◇</div>
                <div className="plan-empty-title">No plans yet</div>
                <div className="plan-empty-body">
                  Plans will appear here after a plan agent finishes or you approve a plan.
                  Create a plan session and start a planning pass to get started.
                </div>
              </div>
            )}
            {sorted.map((plan) => (
              <button
                key={plan.filename}
                type="button"
                className={`plans-list-item ${selected?.filename === plan.filename ? "active" : ""}`}
                onClick={() => openPlan(plan)}
              >
                <div className="plans-item-top">
                  <span className={`plans-status-badge ${plan.session?.status || "unknown"}`}>
                    {plan.session?.status || "archived"}
                  </span>
                  <span className="summary-subtle">{formatRelative(plan.created_at)}</span>
                </div>
                <div className="plans-item-title">{plan.title || plan.filename}</div>
                <div className="plans-item-preview">{plan.preview.slice(0, 100)}</div>
                <div className="plans-item-meta">
                  {plan.session && (
                    <span style={{ color: "var(--accent)", fontWeight: 500 }}>
                      {plan.session.agent_name}
                    </span>
                  )}
                  <span>{formatSize(plan.size_bytes)}</span>
                </div>
              </button>
            ))}
          </div>

          {/* Detail */}
          <div className="plans-detail">
            {detailLoading && (
              <div style={{ padding: 40, textAlign: "center", color: "var(--text-muted)" }}>Loading plan…</div>
            )}
            {!detailLoading && !selected && (
              <div className="plan-empty">
                <div className="plan-empty-icon">◇</div>
                <div className="plan-empty-title">Select a plan</div>
                <div className="plan-empty-body">Pick a plan from the catalog to inspect its full content.</div>
              </div>
            )}

            {selected && (
              <>
                <div className="plans-detail-header">
                  <div>
                    <div className="summary-label">Plan Detail</div>
                    <h3 className="plans-detail-title">{selected.title || selected.filename}</h3>
                  </div>
                  <div style={{ display: "flex", gap: 6 }}>
                    {selected.session && (
                      <button
                        className="btn-secondary"
                        type="button"
                        style={{ fontSize: 12, padding: "4px 10px" }}
                        onClick={() => openSession(selected.session!.id)}
                      >
                        Open Session
                      </button>
                    )}
                    {!editing ? (
                      <>
                        <button
                          className="btn-ghost"
                          type="button"
                          style={{ fontSize: 12, padding: "4px 10px" }}
                          onClick={() => { setEditing(true); setEditContent(selected.content); }}
                        >
                          Edit
                        </button>
                        <button
                          className="btn-ghost"
                          type="button"
                          style={{ fontSize: 12, padding: "4px 10px", color: "var(--error)", borderColor: "var(--error)" }}
                          onClick={() => setConfirmDelete(true)}
                        >
                          Delete
                        </button>
                      </>
                    ) : (
                      <>
                        <button
                          className="btn-primary"
                          type="button"
                          style={{ fontSize: 12, padding: "4px 10px" }}
                          disabled={saving}
                          onClick={async () => {
                            setSaving(true);
                            try {
                              await updatePlan(selected.filename, editContent);
                              setSelected({ ...selected, content: editContent, size_bytes: new TextEncoder().encode(editContent).length });
                              setEditing(false);
                              showToast("Plan updated");
                              load();
                            } catch { showToast("Failed to update"); }
                            finally { setSaving(false); }
                          }}
                        >
                          {saving ? "Saving…" : "Save"}
                        </button>
                        <button
                          className="btn-ghost"
                          type="button"
                          style={{ fontSize: 12, padding: "4px 10px" }}
                          disabled={saving}
                          onClick={() => { setEditing(false); setEditContent(""); }}
                        >
                          Cancel
                        </button>
                      </>
                    )}
                  </div>
                </div>

                <div className="plans-detail-meta">
                  <div className="memory-detail-stat">
                    <span>Created</span>
                    <strong>{formatDate(selected.created_at)}</strong>
                  </div>
                  <div className="memory-detail-stat">
                    <span>Size</span>
                    <strong>{formatSize(selected.size_bytes)}</strong>
                  </div>
                  {selected.session && (
                    <>
                      <div className="memory-detail-stat">
                        <span>Agent</span>
                        <strong>{selected.session.agent_name}</strong>
                      </div>
                      <div className="memory-detail-stat">
                        <span>Session</span>
                        <strong style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>
                          {selected.session.id.slice(0, 12)}
                        </strong>
                      </div>
                    </>
                  )}
                </div>

                <div className="plan-scroll" style={{ marginTop: 12, maxHeight: editing ? "calc(100vh - 440px)" : "calc(100vh - 320px)" }}>
                  {editing ? (
                    <textarea
                      style={{
                        width: "100%", minHeight: 300,
                        padding: "12px 14px", borderRadius: 6,
                        border: "1px solid var(--border)",
                        background: "var(--bg)", color: "var(--text)",
                        fontFamily: "var(--font-mono)", fontSize: 13,
                        resize: "vertical",
                      }}
                      value={editContent}
                      onChange={(e) => setEditContent(e.target.value)}
                    />
                  ) : (
                    <MarkdownRenderer className="plan-pre" content={selected.content} />
                  )}
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      {toast && <div className="toast">{toast}</div>}

      <ConfirmModal
        open={confirmDelete}
        title="Delete plan"
        message={`Permanently delete "${selected?.title || selected?.filename || ""}"? This cannot be undone.`}
        confirmLabel="Delete"
        danger
        loading={deleting}
        onConfirm={async () => {
          if (!selected) return;
          setDeleting(true);
          try {
            await deletePlan(selected.filename);
            showToast("Plan deleted");
            setSelected(null);
            load();
          } catch { showToast("Failed to delete"); }
          finally { setDeleting(false); setConfirmDelete(false); }
        }}
        onCancel={() => setConfirmDelete(false)}
      />
    </section>
  );
}
