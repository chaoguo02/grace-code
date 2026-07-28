import { useCallback, useEffect, useMemo, useState } from "react";
import {
  getMemorySnapshot,
  getMemoryDetail,
  deleteMemory,
  createMemory,
  updateMemory,
  getSessionMemoryRecalls,
  getMemoryEdges,
  getMemoryRevisions,
  previewSessionMemoryRecall,
  getSessionGeneratedMemories,
  setSessionMemoryOverride,
} from "../api/memory";
import { useSessionStore } from "../stores/sessionStore";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { ConfirmModal } from "./ConfirmModal";
import type { MemoryItem, MemoryLayer, MemoryRecallItem, MemoryResponse, MemoryScope, MemoryStatus, MemoryType } from "../types/memory";

type FilterValue = "all" | MemoryType;

function formatDate(ts?: string) {
  if (!ts) return "—";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return ts;
  return date.toLocaleString();
}

function formatRelative(ts?: string) {
  if (!ts) return "No timestamp";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return ts;
  const diffMs = Date.now() - date.getTime();
  const diffMin = Math.round(diffMs / 60000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHour = Math.round(diffMin / 60);
  if (diffHour < 24) return `${diffHour}h ago`;
  const diffDay = Math.round(diffHour / 24);
  return `${diffDay}d ago`;
}

function formatTtl(seconds?: number | null) {
  if (!seconds) return "Permanent";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min TTL`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} hr TTL`;
  return `${Math.round(seconds / 86400)} day TTL`;
}

function toneClass(value: MemoryType | MemoryStatus | MemoryScope | MemoryLayer) {
  return `tone-${value}`;
}

function MemoryMetric({
  label,
  value,
  hint,
}: {
  label: string;
  value: number | string;
  hint: string;
}) {
  return (
    <div className="memory-metric-card">
      <div className="memory-metric-label">{label}</div>
      <div className="memory-metric-value">{value}</div>
      <div className="memory-metric-hint">{hint}</div>
    </div>
  );
}

function DistributionBlock({
  title,
  items,
}: {
  title: string;
  items: Array<{ label: string; value: number; tone: string }>;
}) {
  return (
    <div className="memory-side-card">
      <div className="memory-side-title">{title}</div>
      <div className="memory-distribution-list">
        {items.map((item) => (
          <div className="memory-distribution-row" key={item.label}>
            <div className="memory-distribution-label">
              <span className={`memory-tone-dot ${item.tone}`} />
              {item.label}
            </div>
            <strong>{item.value}</strong>
          </div>
        ))}
      </div>
    </div>
  );
}

export function MemoryView() {
  const activeId = useSessionStore((s) => s.activeId);
  const activeDetail = useSessionStore((s) => s.activeDetail);
  const [snapshot, setSnapshot] = useState<MemoryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState<FilterValue>("all");
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [selectedDetail, setSelectedDetail] = useState<Record<string, unknown> | null>(null);
  const [showNewModal, setShowNewModal] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [newContent, setNewContent] = useState("");
  const [newType, setNewType] = useState<string>("project");
  const [newTTL, setNewTTL] = useState<string>("permanent");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editDesc, setEditDesc] = useState("");
  const [editContent, setEditContent] = useState("");
  const [editConfidence, setEditConfidence] = useState(0);
  const [saving, setSaving] = useState(false);
  const [confirmCancelEdit, setConfirmCancelEdit] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [memoryEdges, setMemoryEdges] = useState<Array<{ source_name: string; target_name: string; relation_type: string; confidence: number; evidence: string }>>([]);
  const [memoryRevisions, setMemoryRevisions] = useState<Array<{ revision: number; content_hash: string; payload: Record<string, unknown>; created_at: string }>>([]);
  const [toast, setToast] = useState<string | null>(null);
  const [recalls, setRecalls] = useState<MemoryRecallItem[]>([]);
  const [generated, setGenerated] = useState<MemoryItem[]>([]);
  const [previewQuery, setPreviewQuery] = useState("");
  const [previewRecalls, setPreviewRecalls] = useState<MemoryRecallItem[]>([]);
  const [recallLoading, setRecallLoading] = useState(false);

  const showToast = (msg: string) => { setToast(msg); setTimeout(() => setToast(null), 3000); };

  const loadData = useCallback(() => {
    setLoading(true);
    getMemorySnapshot()
      .then((data) => {
        setSnapshot(data);
        setSelectedName((current) => current ?? data.items[0]?.name ?? null);
      })
      .catch(() => showToast("Failed to load memories"))
      .finally(() => setLoading(false));
  }, []);

  // Derived detail fields (typed)
  const detailAnchors = selectedDetail && Array.isArray((selectedDetail as Record<string, unknown>).anchors)
    ? (selectedDetail as Record<string, unknown>).anchors as Array<Record<string, unknown>>
    : null;
  const detailContent = selectedDetail?.content as string | undefined;
  const detailSource = selectedDetail?.source as string | undefined;
  const detailSessionId = selectedDetail?.source_session_id as string | undefined;

  // Fetch full detail when a memory is selected
  useEffect(() => {
    if (!selectedName) { setSelectedDetail(null); setMemoryEdges([]); setMemoryRevisions([]); return; }
    getMemoryDetail(selectedName).then(setSelectedDetail).catch(() => {});
    getMemoryEdges(selectedName).then(setMemoryEdges).catch(() => setMemoryEdges([]));
    getMemoryRevisions(selectedName).then(setMemoryRevisions).catch(() => setMemoryRevisions([]));
  }, [selectedName]);

  useEffect(() => { loadData(); }, [loadData]);

  const loadSessionMemory = useCallback(() => {
    if (!activeId) {
      setRecalls([]);
      setGenerated([]);
      return;
    }
    setRecallLoading(true);
    Promise.all([
      getSessionMemoryRecalls(activeId),
      getSessionGeneratedMemories(activeId),
    ])
      .then(([recallResp, generatedResp]) => {
        setRecalls(recallResp.items || []);
        setGenerated(generatedResp.items || []);
      })
      .catch(() => showToast("Failed to load session memory recall"))
      .finally(() => setRecallLoading(false));
  }, [activeId]);

  useEffect(() => { loadSessionMemory(); }, [loadSessionMemory]);

  const filteredItems = useMemo(() => {
    const items = snapshot?.items ?? [];
    return items.filter((item) => {
      const matchesType = typeFilter === "all" || item.type === typeFilter;
      const text = `${item.name} ${item.description}`.toLowerCase();
      const matchesQuery = !query.trim() || text.includes(query.trim().toLowerCase());
      return matchesType && matchesQuery;
    });
  }, [snapshot, typeFilter, query]);

  const selected = filteredItems.find((item) => item.name === selectedName) ?? filteredItems[0] ?? null;

  const latestRecalls = useMemo(() => recalls.slice(0, 8), [recalls]);
  const injectedRecallCount = useMemo(() => recalls.filter((item) => item.injected).length, [recalls]);
  const omittedRecalls = useMemo(() => recalls.filter((item) => !item.injected), [recalls]);
  const [showOmitted, setShowOmitted] = useState(false);

  // Group by type for catalog display (when no type filter is active)
  const groupedByType = useMemo(() => {
    if (typeFilter !== "all") return null; // already filtered, no grouping needed
    const types: MemoryType[] = ["user", "feedback", "project", "reference"];
    return types
      .map((t) => ({ type: t, items: filteredItems.filter((item) => item.type === t) }))
      .filter((g) => g.items.length > 0);
  }, [filteredItems, typeFilter]);

  useEffect(() => {
    if (!selected && filteredItems[0]) setSelectedName(filteredItems[0].name);
  }, [filteredItems, selected]);

  const typeDistribution = useMemo(() => {
    const byType = snapshot?.overview.by_type;
    if (!byType) return [];
    return [
      { label: "User", value: byType.user, tone: toneClass("user") },
      { label: "Feedback", value: byType.feedback, tone: toneClass("feedback") },
      { label: "Project", value: byType.project, tone: toneClass("project") },
      { label: "Reference", value: byType.reference, tone: toneClass("reference") },
    ];
  }, [snapshot]);

  const scopeDistribution = useMemo(() => {
    const byScope = snapshot?.overview.by_scope;
    if (!byScope) return [];
    return [
      { label: "Global", value: byScope.global, tone: toneClass("global") },
      { label: "Project", value: byScope.project, tone: toneClass("project") },
      { label: "Session", value: byScope.session, tone: toneClass("session") },
    ];
  }, [snapshot]);

  const layerDistribution = useMemo(() => {
    const byLayer = snapshot?.overview.by_layer;
    if (!byLayer) return [];
    return [
      { label: "Project Store", value: byLayer.project, tone: toneClass("project") },
      { label: "Global Store", value: byLayer.global, tone: toneClass("global") },
      { label: "Archive", value: byLayer.archive, tone: toneClass("archive") },
    ];
  }, [snapshot]);

  return (
    <section className="view active" data-view-name="memory">
      <div className="memory-page">
        <div className="memory-hero">
          <div className="memory-hero-copy">
            <div className="summary-label">Memory Workspace</div>
            <h2 className="memory-hero-title">Persistent memory, made inspectable</h2>
            <p className="memory-hero-body">
              Surface user, feedback, project, and reference memories in one place, with lifecycle,
              scope, confidence, and retention visible instead of hidden behind MEMORY.md files.
            </p>
            <div className="memory-hero-chips">
              <span className="trace-pill">Two-tier store</span>
              <span className="trace-pill">Typed memory model</span>
              <span className="trace-pill">Archive-aware</span>
              {snapshot?.overview.preview && <span className="trace-pill warning">Preview data</span>}
            </div>
          </div>
          <div className="memory-context-card">
            <div className="memory-context-label">Current session context</div>
            <div className="memory-context-title">{activeDetail?.title || "No active session selected"}</div>
            <div className="memory-context-meta">
              <span>{activeDetail?.agent_name || "grace"}</span>
              <span>{activeDetail?.status || "idle"}</span>
              <span>{activeDetail?.mode || "chat"}</span>
            </div>
            <div className="memory-context-note">
              Memory routing in your code distinguishes global vs project storage, and injects by typed policy rather than a flat blob.
            </div>
          </div>
        </div>

        <div className="memory-metric-grid">
          <MemoryMetric label="Total Memories" value={snapshot?.overview.total ?? "—"} hint="All visible memory records" />
          <MemoryMetric label="Active" value={snapshot?.overview.active ?? "—"} hint="Injected or available to retrieval" />
          <MemoryMetric label="Deprecated" value={snapshot?.overview.deprecated ?? "—"} hint="Kept for audit, excluded from normal injection" />
          <MemoryMetric label="Archived" value={snapshot?.overview.archived ?? "—"} hint="Moved out of active store" />
          <MemoryMetric label="Expiring Soon" value={snapshot?.overview.expiring ?? "—"} hint="TTL-bound entries nearing expiry" />
        </div>

        <div className="memory-toolbar">
          <div className="memory-search">
            <span className="memory-search-icon">/</span>
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search memories by name or description..."
            />
          </div>
          <div className="memory-filter-group">
            {(["all", "user", "feedback", "project", "reference"] as const).map((value) => (
              <button
                key={value}
                type="button"
                className={`memory-filter-chip ${typeFilter === value ? "active" : ""}`}
                onClick={() => setTypeFilter(value)}
              >
                {value === "all" ? "All" : value}
              </button>
            ))}
            <button className="btn-primary" type="button" onClick={() => setShowNewModal(true)}
              style={{ marginLeft: 8, padding: "4px 12px", fontSize: 12 }}>
              + New
            </button>
            <button className="btn-ghost" type="button" onClick={loadData}
              style={{ padding: "4px 10px", fontSize: 12 }} title="Refresh">
              ↻
            </button>
          </div>
        </div>

        <div className="memory-layout">
          <div className="memory-catalog">
            <div className="memory-panel-head">
              <div>
                <div className="summary-label">Catalog</div>
                <h3 className="memory-panel-title">Memory inventory</h3>
              </div>
              <span className="summary-subtle">{filteredItems.length} visible</span>
            </div>

            <div className="memory-catalog-list">
              {loading && (
                <div style={{ padding: "12px 6px" }}>
                  <div className="skeleton-line" style={{ width: "40%" }} />
                  <div className="skeleton-line" />
                  <div className="skeleton-line" style={{ width: "70%" }} />
                  <div className="skeleton-line" />
                  <div className="skeleton-line" style={{ width: "55%" }} />
                </div>
              )}
              {!loading && filteredItems.length === 0 && !query && typeFilter === "all" && (
                <div className="memory-empty">
                  <strong>No memories yet</strong>
                  <p style={{ margin: "8px 0", fontSize: 13, color: "var(--text-muted)" }}>
                    Create one using the <strong>+ New</strong> button above,
                    or use the agent&apos;s <code>memory_write</code> tool during a chat session.
                  </p>
                  <button className="btn-primary" type="button" onClick={() => setShowNewModal(true)}
                    style={{ padding: "6px 16px", fontSize: 13 }}>
                    + Create your first memory
                  </button>
                </div>
              )}
              {!loading && filteredItems.length === 0 && (query || typeFilter !== "all") && (
                <div className="memory-empty">No memories match the current filters. Try a different search or filter.</div>
              )}

              {/* Grouped by type (when no filter) or flat list */}
              {groupedByType
                ? groupedByType.map((group) => (
                    <div key={group.type} style={{ marginBottom: 4 }}>
                      <div className="memory-group-header" style={{
                        padding: "6px 10px", fontSize: 11, fontWeight: 700,
                        color: "var(--text-muted)", textTransform: "uppercase",
                        letterSpacing: "0.5px", borderBottom: "1px solid var(--border)",
                        background: "var(--bg-elev)", position: "sticky", top: 0, zIndex: 1,
                      }}>
                        <span className={`memory-tone-dot ${toneClass(group.type)}`} style={{ marginRight: 6 }} />
                        {group.type} · {group.items.length}
                      </div>
                      {group.items.map((item) => (
                        <button
                          key={item.name}
                          type="button"
                          className={`memory-list-item ${selected?.name === item.name ? "active" : ""}`}
                          onClick={() => setSelectedName(item.name)}
                        >
                          <div className="memory-list-top">
                            <span className={`memory-badge subtle ${toneClass(item.status)}`}>{item.status}</span>
                            <span className="summary-subtle">{formatRelative(item.updated_at)}</span>
                          </div>
                          <div className="memory-list-name">{item.name}</div>
                          <div className="memory-list-description">{item.description}</div>
                          <div className="memory-list-meta">
                            <span>{item.scope}</span>
                            <span>{item.created_at ? formatRelative(item.created_at) : ""}</span>
                            <span>{item.access_count} reads</span>
                          </div>
                        </button>
                      ))}
                    </div>
                  ))
                : filteredItems.map((item) => (
                    <button
                      key={item.name}
                      type="button"
                      className={`memory-list-item ${selected?.name === item.name ? "active" : ""}`}
                      onClick={() => setSelectedName(item.name)}
                    >
                      <div className="memory-list-top">
                        <span className={`memory-badge ${toneClass(item.type)}`}>{item.type}</span>
                        <span className={`memory-badge subtle ${toneClass(item.status)}`}>{item.status}</span>
                        <span className="summary-subtle">{formatRelative(item.updated_at)}</span>
                      </div>
                      <div className="memory-list-name">{item.name}</div>
                      <div className="memory-list-description">{item.description}</div>
                      <div className="memory-list-meta">
                        <span>{item.scope}</span>
                        <span>{item.created_at ? formatRelative(item.created_at) : ""}</span>
                        <span>{item.access_count} reads</span>
                      </div>
                    </button>
                  ))}
            </div>
          </div>

          <div className="memory-detail">
            <div className="memory-panel-head">
              <div>
                <div className="summary-label">Detail</div>
                <h3 className="memory-panel-title">{selected?.name || "Select a memory"}</h3>
              </div>
              {selected && <span className={`memory-badge ${toneClass(selected.type)}`}>{selected.type}</span>}
            </div>

            {!selected && <div className="memory-empty">Pick a memory from the catalog to inspect its metadata and content.</div>}

            {selected && (
              <>
                {editing ? (
                  <input type="text" value={editDesc} onChange={(e) => setEditDesc(e.target.value)}
                    style={{ width: "100%", padding: "8px 12px", borderRadius: 6, border: "1px solid var(--border)", fontSize: 13, marginBottom: 12 }} />
                ) : (
                  <p className="memory-detail-description">{selected.description}</p>
                )}

                <div className="memory-detail-grid">
                  <div className="memory-detail-stat">
                    <span>Scope</span>
                    <strong>{selected.scope}</strong>
                  </div>
                  <div className="memory-detail-stat">
                    <span>Status</span>
                    <strong>{selected.status}</strong>
                  </div>
                  <div className="memory-detail-stat">
                    <span>Confidence</span>
                    {editing ? (
                      <input className="form-range" type="range" min="0" max="100" value={Math.round(editConfidence * 100)}
                        onChange={(e) => setEditConfidence(Number(e.target.value) / 100)} />
                    ) : (
                      <strong>{Math.round(selected.confidence * 100)}%</strong>
                    )}
                  </div>
                  <div className="memory-detail-stat">
                    <span>Access count</span>
                    <strong>{selected.access_count ?? 0}</strong>
                  </div>
                </div>

                <div className="memory-preview-card">
                  <div className="memory-preview-label">Content</div>
                  {editing ? (
                    <textarea value={editContent} onChange={(e) => setEditContent(e.target.value)}
                      rows={8} style={{ width: "100%", padding: "8px 12px", borderRadius: 6, border: "1px solid var(--border)", fontSize: 13, fontFamily: "var(--font-mono)", resize: "vertical", marginTop: 6 }} />
                  ) : (
                    <MarkdownRenderer className="memory-preview-body" content={detailContent || "Loading..."} />
                  )}
                </div>

                {(() => {
                  // Extract **Confidence reason:** block from content
                  const content = detailContent || "";
                  const m = content.match(/\*\*Confidence reason:\*\*\s*(.+?)(?:\n|$)/);
                  if (!m) return null;
                  const reason = m[1].trim();
                  return (
                    <div className="memory-preview-card" style={{ marginTop: 8, borderColor: "var(--accent-soft)", background: "var(--bg-elev)" }}>
                      <div className="memory-preview-label" style={{ color: "var(--accent)" }}>Confidence reason</div>
                      <p style={{ margin: "6px 0 0", fontSize: 13, color: "var(--text-dim)", fontStyle: "italic", lineHeight: 1.5 }}>
                        {reason}
                      </p>
                    </div>
                  );
                })()}

                {detailAnchors && detailAnchors.length > 0 && (
                  <div className="memory-preview-card" style={{ marginTop: 8 }}>
                    <div className="memory-preview-label">Anchors ({detailAnchors.length})</div>
                    <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 6 }}>
                      {detailAnchors.map((a, i) => (
                        <div key={i} style={{ fontSize: 12, color: "var(--text-dim)", fontFamily: "var(--font-mono)" }}>
                          <span style={{ color: "var(--accent)" }}>{String(a.kind || "")}</span>
                          {!!a.path && <span>: {String(a.path)}</span>}
                          {!!a.name && <span>: {String(a.name)}</span>}
                          {!!a.content_hash && <span style={{ color: "var(--text-muted)", marginLeft: 8 }}>hash:{String(a.content_hash).slice(0, 12)}</span>}
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Entity Links */}
                {memoryEdges.length > 0 && (
                  <div className="memory-preview-card" style={{ marginTop: 8 }}>
                    <div className="memory-preview-label">Linked Memories ({memoryEdges.length})</div>
                    <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 6 }}>
                      {memoryEdges.map((edge, i) => (
                        <div key={i} style={{ fontSize: 12, color: "var(--text-dim)", display: "flex", gap: 8, alignItems: "center" }}>
                          <span style={{ color: "var(--accent)", fontWeight: 500 }}>{edge.relation_type}</span>
                          <span>{edge.source_name === selected.name ? edge.target_name : edge.source_name}</span>
                          <span style={{ fontSize: 10, color: "var(--text-muted)" }}>{(edge.confidence * 100).toFixed(0)}%</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Revision History */}
                {memoryRevisions.length > 1 && (
                  <div className="memory-preview-card" style={{ marginTop: 8 }}>
                    <div className="memory-preview-label">Revision History ({memoryRevisions.length})</div>
                    <div style={{ maxHeight: 120, overflow: "auto", marginTop: 4 }}>
                      {memoryRevisions.slice(0, 5).map((rev, i) => (
                        <div key={i} style={{ fontSize: 11, color: "var(--text-dim)", padding: "2px 0", borderBottom: i < Math.min(memoryRevisions.length, 5) - 1 ? "1px solid var(--border)" : "none" }}>
                          <span style={{ fontWeight: 600, color: "var(--text)" }}>v{rev.revision}</span>
                          <span style={{ marginLeft: 8 }}>{new Date(rev.created_at).toLocaleString()}</span>
                          <span style={{ marginLeft: 8, fontFamily: "var(--font-mono)", fontSize: 9, color: "var(--text-muted)" }}>{rev.content_hash.slice(0, 8)}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
                  {!editing ? (
                    <button className="btn-ghost" type="button"
                      onClick={() => {
                        setEditDesc(selected.description);
                        setEditContent(detailContent || "");
                        setEditConfidence(selected.confidence);
                        setEditing(true);
                      }}>
                      Edit
                    </button>
                  ) : (
                    <>
                      <button className="btn-primary" type="button" disabled={saving}
                        onClick={async () => {
                          setSaving(true);
                          try {
                            await updateMemory(selected.name, {
                              description: editDesc, content: editContent,
                              confidence: editConfidence,
                            });
                            setEditing(false);
                            showToast("Memory updated");
                            loadData();
                            if (selectedName) getMemoryDetail(selectedName).then(setSelectedDetail).catch(() => {});
                          } catch { showToast("Failed to update"); }
                          finally { setSaving(false); }
                        }}
                        style={{ padding: "6px 14px", fontSize: 13 }}>
                        {saving ? "Saving..." : "Save"}
                      </button>
                      <button className="btn-ghost" type="button" disabled={saving}
                        onClick={() => {
                          if (editDesc !== selected.description || editContent !== detailContent || editConfidence !== selected.confidence) {
                            setConfirmCancelEdit(true);
                          } else {
                            setEditing(false);
                          }
                        }}
                        style={{ padding: "6px 14px", fontSize: 13 }}>
                        Cancel
                      </button>
                    </>
                  )}
                  <button className="btn-ghost" type="button"
                    onClick={() => setConfirmDelete(selected.name)}
                    style={{ color: "var(--error)", borderColor: "var(--error)" }}>
                    Delete
                  </button>
                </div>

                <div className="memory-meta-grid">
                  <div className="memory-meta-card">
                    <div className="memory-meta-label">Created</div>
                    <div className="memory-meta-value">{formatDate(selected.created_at || selectedDetail?.created_at as string | undefined)}</div>
                  </div>
                  <div className="memory-meta-card">
                    <div className="memory-meta-label">Updated</div>
                    <div className="memory-meta-value">{formatDate(selected.updated_at)}</div>
                  </div>
                  {!!detailSource && (
                    <div className="memory-meta-card">
                      <div className="memory-meta-label">Source</div>
                      <div className="memory-meta-value">{detailSource}</div>
                    </div>
                  )}
                  {!!detailSessionId && (
                    <div className="memory-meta-card">
                      <div className="memory-meta-label">Session</div>
                      <div className="memory-meta-value" style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>
                        {detailSessionId.slice(0, 12)}
                      </div>
                    </div>
                  )}
                </div>
              </>
            )}
          </div>

          <div className="memory-side">
            <DistributionBlock title="By type" items={typeDistribution} />
            <DistributionBlock title="By scope" items={scopeDistribution} />
            <DistributionBlock title="By storage layer" items={layerDistribution} />

            <div className="memory-side-card">
              <div className="memory-side-title">Current session recall</div>
              <div className="summary-subtle" style={{ marginBottom: 8 }}>
                {activeId ? `${injectedRecallCount} injected · ${recalls.length} recorded` : "Open a session to inspect recall"}
              </div>
              {recallLoading && <div className="memory-empty">Loading recall records...</div>}
              {!recallLoading && latestRecalls.length === 0 && <div className="memory-empty">No recall records for this session yet.</div>}
              {latestRecalls.map((item) => (
                <div key={`${item.created_at}-${item.memory_name}`} className="memory-list-item" style={{ cursor: "default" }}>
                  <div className="memory-list-top">
                    <span className={`memory-badge ${item.injected ? "tone-project" : "tone-archive"}`}>{item.injected ? "injected" : item.omitted_reason || "omitted"}</span>
                    <span className="summary-subtle">{item.source} · {Math.round(item.score * 100)}%</span>
                  </div>
                  <div className="memory-list-name">{item.memory_name}</div>
                  <div className="memory-list-description">{item.reason}</div>
                  <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
                    <button className="btn-ghost" type="button" disabled={!activeId}
                      onClick={async () => {
                        if (!activeId) return;
                        await setSessionMemoryOverride(activeId, item.memory_name, item.override === "pin" ? "unpin" : "pin");
                        showToast(item.override === "pin" ? "Memory unpinned" : "Memory pinned");
                        loadSessionMemory();
                      }} style={{ padding: "3px 8px", fontSize: 11 }}>
                      {item.override === "pin" ? "Unpin" : "Pin"}
                    </button>
                    <button className="btn-ghost" type="button" disabled={!activeId}
                      onClick={async () => {
                        if (!activeId) return;
                        await setSessionMemoryOverride(activeId, item.memory_name, item.override === "disable" ? "enable" : "disable");
                        showToast(item.override === "disable" ? "Memory enabled" : "Memory disabled");
                        loadSessionMemory();
                      }} style={{ padding: "3px 8px", fontSize: 11 }}>
                      {item.override === "disable" ? "Enable" : "Disable"}
                    </button>
                  </div>
                </div>
              ))}
              {/* Omitted candidates: collapsed section */}
              {omittedRecalls.length > 0 && (
                <div style={{ marginTop: 8 }}>
                  <button
                    type="button"
                    className="btn-ghost"
                    onClick={() => setShowOmitted((v) => !v)}
                    style={{ padding: "4px 10px", fontSize: 11, width: "100%", textAlign: "left", display: "flex", alignItems: "center", gap: 6 }}
                  >
                    <span style={{ fontWeight: 600, fontSize: 12 }}>
                      {showOmitted ? "▾" : "▸"} Omitted candidates ({omittedRecalls.length})
                    </span>
                  </button>
                  {showOmitted && (
                    <div style={{ marginTop: 4 }}>
                      {omittedRecalls.map((item) => (
                        <div key={`omitted-${item.created_at}-${item.memory_name}`} className="memory-list-item" style={{ cursor: "default", opacity: 0.7 }}>
                          <div className="memory-list-top">
                            <span className="memory-badge tone-archive">{item.omitted_reason || "omitted"}</span>
                            <span className="summary-subtle">{item.source} · {Math.round(item.score * 100)}%</span>
                          </div>
                          <div className="memory-list-name">{item.memory_name}</div>
                          <div className="memory-list-description">{item.description || item.reason}</div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
              <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 6 }}>
                <input className="form-input" value={previewQuery} onChange={(e) => setPreviewQuery(e.target.value)}
                  placeholder="Preview recall query..." disabled={!activeId} />
                <button className="btn-ghost" type="button" disabled={!activeId || !previewQuery.trim()}
                  onClick={async () => {
                    if (!activeId) return;
                    const resp = await previewSessionMemoryRecall(activeId, previewQuery);
                    setPreviewRecalls(resp.items || resp.records || []);
                  }} style={{ padding: "5px 10px", fontSize: 12 }}>
                  Preview recall
                </button>
              </div>
              {previewRecalls.slice(0, 5).map((item) => (
                <div key={`preview-${item.memory_name}`} className="memory-list-meta" style={{ marginTop: 6 }}>
                  <span>{item.memory_name}</span><span>{item.source}</span><span>{Math.round(item.score * 100)}%</span>
                </div>
              ))}
            </div>

            <div className="memory-side-card">
              <div className="memory-side-title">Generated from this session</div>
              {generated.length === 0 && <div className="memory-empty">No generated memories linked to this session.</div>}
              {generated.slice(0, 6).map((item) => (
                <button key={item.name} type="button" className="memory-list-item" onClick={() => setSelectedName(item.name)}>
                  <div className="memory-list-top">
                    <span className={`memory-badge ${toneClass(item.type)}`}>{item.type}</span>
                    <span className="summary-subtle">{formatRelative(item.created_at)}</span>
                  </div>
                  <div className="memory-list-name">{item.name}</div>
                  <div className="memory-list-description">{item.description}</div>
                </button>
              ))}
            </div>

            <div className="memory-side-card">
              <div className="memory-side-title">What the code already supports</div>
              <ul className="memory-architecture-list">
                <li>Typed categories: user, feedback, project, reference</li>
                <li>Lifecycle states: active and deprecated</li>
                <li>Scope routing: session, project, global</li>
                <li>Two-tier storage with archive separation</li>
                <li>TTL, validation, access count, and anchor metadata</li>
              </ul>
            </div>
          </div>
        </div>
      </div>

      {/* Toast notification */}
      {toast && <div className="toast">{toast}</div>}

      {/* Delete confirmation */}
      <ConfirmModal
        open={!!confirmDelete}
        title="Delete memory"
        message={`Permanently delete "${confirmDelete || ""}"? This cannot be undone.`}
        confirmLabel="Delete"
        danger
        loading={deleting}
        onConfirm={async () => {
          if (!confirmDelete) return;
          setDeleting(true);
          try {
            await deleteMemory(confirmDelete);
            setSelectedName(null);
            showToast("Memory deleted");
            loadData();
          } catch { showToast("Failed to delete"); }
          finally { setDeleting(false); setConfirmDelete(null); }
        }}
        onCancel={() => setConfirmDelete(null)}
      />

      {/* Confirm cancel edit */}
      <ConfirmModal
        open={confirmCancelEdit}
        title="Discard changes?"
        message="You have unsaved changes. Discard them?"
        confirmLabel="Discard"
        danger
        onConfirm={() => { setConfirmCancelEdit(false); setEditing(false); }}
        onCancel={() => setConfirmCancelEdit(false)}
      />

      {/* New memory modal */}
      {showNewModal && (
        <div className="modal-overlay" onKeyDown={(e) => e.key === "Escape" && !creating && setShowNewModal(false)}>
          <div className="modal-box">
            <h3>New Memory</h3>
            <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              <div>
                <label>Name (slug) <span style={{ color: "var(--error)" }}>*</span></label>
                <input className="form-input" type="text" value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  placeholder="e.g. build-commands" autoFocus />
                {newName.trim() && !/^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$/.test(newName.trim()) && (
                  <div className="form-error">Only lowercase letters, numbers, hyphens, underscores.</div>
                )}
              </div>
              <div>
                <label>Description <span style={{ color: "var(--error)" }}>*</span></label>
                <input className="form-input" type="text" value={newDesc}
                  onChange={(e) => setNewDesc(e.target.value)}
                  placeholder="One-line summary" />
              </div>
              <div>
                <label>Type</label>
                <select className="form-select" value={newType} onChange={(e) => setNewType(e.target.value)}>
                  <option value="project">Project</option>
                  <option value="reference">Reference</option>
                  <option value="user">User</option>
                  <option value="feedback">Feedback</option>
                </select>
              </div>
              <div>
                <label>TTL (auto-expire)</label>
                <select className="form-select" value={newTTL} onChange={(e) => setNewTTL(e.target.value)}>
                  <option value="permanent">Permanent</option>
                  <option value="3600">1 hour</option>
                  <option value="86400">1 day</option>
                  <option value="604800">7 days</option>
                  <option value="2592000">30 days</option>
                </select>
              </div>
              <div>
                <label>Content (Markdown)</label>
                <textarea className="form-textarea" value={newContent}
                  onChange={(e) => setNewContent(e.target.value)}
                  rows={6} placeholder="## Heading&#10;Content here..." />
              </div>
            </div>
            <div className="modal-actions">
              <button className="btn-ghost" type="button" onClick={() => setShowNewModal(false)}
                disabled={creating}>Cancel</button>
              <button className="btn-primary" type="button"
                disabled={creating || !newName.trim() || !newDesc.trim() || !/^[a-z0-9]/.test(newName.trim())}
                onClick={async () => {
                  setCreating(true);
                  try {
                    await createMemory({ name: newName.trim(), description: newDesc.trim(),
                      content: newContent, type: newType,
                      ttl_seconds: newTTL === "permanent" ? undefined : parseInt(newTTL) });
                    setShowNewModal(false);
                    setNewName(""); setNewDesc(""); setNewContent(""); setNewType("project");
                    showToast("Memory created");
                    loadData();
                  } catch { showToast("Failed to create memory"); }
                  finally { setCreating(false); }
                }}>
                {creating ? "Creating..." : "Create"}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
