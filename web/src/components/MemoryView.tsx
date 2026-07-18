import { useEffect, useMemo, useState } from "react";
import { getMemorySnapshot } from "../api/memory";
import { useSessionStore } from "../stores/sessionStore";
import type { MemoryItem, MemoryLayer, MemoryResponse, MemoryScope, MemoryStatus, MemoryType } from "../types/memory";

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
  const activeDetail = useSessionStore((s) => s.activeDetail);
  const [snapshot, setSnapshot] = useState<MemoryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState<FilterValue>("all");
  const [selectedName, setSelectedName] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    setLoading(true);
    getMemorySnapshot()
      .then((data) => {
        if (!mounted) return;
        setSnapshot(data);
        setSelectedName((current) => current ?? data.items[0]?.name ?? null);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, []);

  const filteredItems = useMemo(() => {
    const items = snapshot?.items ?? [];
    return items.filter((item) => {
      const matchesType = typeFilter === "all" || item.type === typeFilter;
      const text = `${item.name} ${item.description} ${item.preview ?? ""}`.toLowerCase();
      const matchesQuery = !query.trim() || text.includes(query.trim().toLowerCase());
      return matchesType && matchesQuery;
    });
  }, [snapshot, typeFilter, query]);

  const selected = filteredItems.find((item) => item.name === selectedName) ?? filteredItems[0] ?? null;

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
              placeholder="Search memories by name, summary, or preview..."
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
              {loading && <div className="memory-empty">Loading memory snapshot…</div>}
              {!loading && filteredItems.length === 0 && (
                <div className="memory-empty">No memories match the current filters.</div>
              )}

              {filteredItems.map((item) => (
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
                    <span>{item.layer}</span>
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
                <p className="memory-detail-description">{selected.description}</p>

                <div className="memory-detail-grid">
                  <div className="memory-detail-stat">
                    <span>Scope</span>
                    <strong>{selected.scope}</strong>
                  </div>
                  <div className="memory-detail-stat">
                    <span>Layer</span>
                    <strong>{selected.layer}</strong>
                  </div>
                  <div className="memory-detail-stat">
                    <span>Status</span>
                    <strong>{selected.status}</strong>
                  </div>
                  <div className="memory-detail-stat">
                    <span>Confidence</span>
                    <strong>{Math.round(selected.confidence * 100)}%</strong>
                  </div>
                  <div className="memory-detail-stat">
                    <span>TTL</span>
                    <strong>{formatTtl(selected.ttl_seconds)}</strong>
                  </div>
                  <div className="memory-detail-stat">
                    <span>Anchors</span>
                    <strong>{selected.anchors_count}</strong>
                  </div>
                </div>

                <div className="memory-preview-card">
                  <div className="memory-preview-label">Preview</div>
                  <div className="memory-preview-body">
                    {selected.preview || "No preview text available for this memory yet."}
                  </div>
                </div>

                <div className="memory-meta-grid">
                  <div className="memory-meta-card">
                    <div className="memory-meta-label">Updated</div>
                    <div className="memory-meta-value">{formatDate(selected.updated_at)}</div>
                  </div>
                  <div className="memory-meta-card">
                    <div className="memory-meta-label">Validated</div>
                    <div className="memory-meta-value">{formatDate(selected.validated_at)}</div>
                  </div>
                  <div className="memory-meta-card">
                    <div className="memory-meta-label">Expires</div>
                    <div className="memory-meta-value">{formatDate(selected.expires_at)}</div>
                  </div>
                </div>
              </>
            )}
          </div>

          <div className="memory-side">
            <DistributionBlock title="By type" items={typeDistribution} />
            <DistributionBlock title="By scope" items={scopeDistribution} />
            <DistributionBlock title="By storage layer" items={layerDistribution} />

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
    </section>
  );
}
