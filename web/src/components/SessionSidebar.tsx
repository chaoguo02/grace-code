import { useEffect, useState, useCallback } from "react";
import { useSessionStore } from "../stores/sessionStore";
import { useChatStore } from "../stores/chatStore";

function formatRelative(ts?: string | null) {
  if (!ts) return "No activity";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return ts;
  const deltaMin = Math.round((Date.now() - date.getTime()) / 60000);
  if (deltaMin < 1) return "Just now";
  if (deltaMin < 60) return `${deltaMin}m ago`;
  const deltaHour = Math.round(deltaMin / 60);
  if (deltaHour < 24) return `${deltaHour}h ago`;
  const deltaDay = Math.round(deltaHour / 24);
  return `${deltaDay}d ago`;
}

function statusLabel(status: string) {
  if (status === "running") return "Active";
  if (status === "completed") return "Completed";
  if (status === "failed") return "Failed";
  if (status === "queued") return "Queued";
  return status;
}

function statusClass(status: string) {
  if (status === "running") return "status-running";
  if (status === "completed") return "status-completed";
  if (status === "failed") return "status-failed";
  if (status === "queued") return "status-queued";
  return "status-neutral";
}

export function SessionSidebar() {
  const {
    sessions,
    activeId,
    isLoading,
    loadSessions,
    openSession,
    createSession,
    deleteSession,
    deleteSessionsBatch,
  } = useSessionStore();
  const { clear } = useChatStore();
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [batchDeleting, setBatchDeleting] = useState(false);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  useEffect(() => {
    setSelectedIds(new Set());
  }, [sessions.length]);

  const handleOpen = async (id: string) => {
    clear();
    await openSession(id);
  };

  const handleDelete = async (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    if (!confirm("Delete this session?")) return;
    setDeletingId(id);
    if (id === activeId) clear();
    await deleteSession(id);
    setDeletingId(null);
  };

  const toggleSelect = (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const selectAll = () => {
    setSelectedIds(new Set(sessions.map((s) => s.id)));
  };

  const deselectAll = () => {
    setSelectedIds(new Set());
  };

  const handleBatchDelete = useCallback(async () => {
    if (selectedIds.size === 0) return;
    const msg = `Delete ${selectedIds.size} session${selectedIds.size > 1 ? "s" : ""}?`;
    if (!confirm(msg)) return;
    setBatchDeleting(true);
    if (selectedIds.has(activeId ?? "")) clear();
    await deleteSessionsBatch(Array.from(selectedIds));
    setBatchDeleting(false);
  }, [selectedIds, activeId, clear, deleteSessionsBatch]);

  const inBatchMode = selectedIds.size > 0;

  return (
    <aside className="sidebar">
      <div className="sidebar-head">
        <div className="sidebar-head-row">
          <div className="brand">
            <span className="brand-mark">GC</span>
            <span className="brand-name">Grace Code</span>
          </div>
          <button className="sidebar-collapse-btn" type="button" aria-label="Collapse sidebar">
            «
          </button>
        </div>

        <button className="btn-primary sidebar-primary" type="button" onClick={() => createSession()}>
          + New Session
        </button>

        <div className="sidebar-meta sidebar-meta-compact">
          <div className="sidebar-section-label">Sessions</div>
          <div className="sidebar-section-count">{sessions.length}</div>
        </div>
      </div>

      <div className="sidebar-section sidebar-sessions">
        <div className="sidebar-title sidebar-title-tight">
          <span>{isLoading ? "Syncing" : "Sessions"}</span>
          <span>{selectedIds.size > 0 ? `${selectedIds.size} selected` : ""}</span>
        </div>

        <div id="session-list" className="session-list">
          {isLoading && sessions.length === 0 && <div className="empty-state">Loading…</div>}
          {!isLoading && sessions.length === 0 && <div className="empty-state">No sessions yet.</div>}

          {sessions.map((s) => (
            <div
              key={s.id}
              className={`session-item ${s.id === activeId ? "active" : ""}`}
              onClick={() => handleOpen(s.id)}
            >
              <div className="session-mainline">
                <input
                  className="session-checkbox"
                  type="checkbox"
                  checked={selectedIds.has(s.id)}
                  onChange={() => {}}
                  onClick={(e) => toggleSelect(e, s.id)}
                />

                <div className="session-body">
                  <div className="session-headline">
                    <span className={`session-status-dot ${statusClass(s.status)}`} />
                    <span className={`session-status-pill ${statusClass(s.status)}`}>
                      {statusLabel(s.status)}
                    </span>
                    <span className="summary-subtle session-age">{formatRelative(s.updated_at)}</span>
                  </div>

                  <div className="session-preview">
                    {(s.title || s.summary || s.id).slice(0, 42)}
                  </div>

                  <div className="session-meta">
                    <span>{s.agent_name}</span>
                    {s.total_tokens_estimate != null && (
                      <span>{s.total_tokens_estimate.toLocaleString()} tokens</span>
                    )}
                    {s.message_count != null && <span>{s.message_count} steps</span>}
                  </div>
                </div>

                <button
                  className="session-delete"
                  onClick={(e) => handleDelete(e, s.id)}
                  title="Delete session"
                  disabled={deletingId === s.id}
                >
                  {deletingId === s.id ? "…" : "›"}
                </button>
              </div>
            </div>
          ))}
        </div>
      </div>

      {inBatchMode && (
        <div className="batch-toolbar">
          <span className="summary">{selectedIds.size} selected</span>
          <button className="btn-ghost" type="button" onClick={selectAll}>
            All
          </button>
          <button className="btn-ghost" type="button" onClick={deselectAll}>
            None
          </button>
          <button
            className="btn-reject"
            type="button"
            onClick={handleBatchDelete}
            disabled={batchDeleting}
          >
            {batchDeleting ? "Deleting…" : `Delete ${selectedIds.size}`}
          </button>
        </div>
      )}

      <div className="sidebar-user-card">
        <div className="sidebar-user-avatar">A</div>
        <div className="sidebar-user-body">
          <div className="sidebar-user-name">Alex Morgan</div>
          <div className="sidebar-user-email">alex@example.com</div>
        </div>
        <button className="sidebar-user-menu" type="button" aria-label="Account menu">
          ˅
        </button>
      </div>
    </aside>
  );
}
