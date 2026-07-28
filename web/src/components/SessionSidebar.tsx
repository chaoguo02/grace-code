import { useEffect, useState, useCallback } from "react";
import { useSessionStore } from "../stores/sessionStore";
import { SessionStatsDrawer } from "./SessionStatsDrawer";
import { ConfirmModal } from "./ConfirmModal";
import { getAppDefaults } from "../api/config";
import { updateSession } from "../api/sessions";
import type { SessionSummary } from "../types";
import { summarizeStatus } from "../utils/status";

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

function statusClass(status: string) {
  if (status === "running") return "status-running";
  if (status === "completed") return "status-completed";
  if (status === "failed") return "status-failed";
  if (status === "queued") return "status-queued";
  return "status-neutral";
}

export function SessionSidebar({
  onToggleCollapse,
  onOpenSession,
}: {
  onToggleCollapse?: () => void;
  onOpenSession?: (sessionId: string) => void;
}) {
  const {
    sessions,
    activeId,
    isLoading,
    error: storeError,
    loadSessions,
    openSession,
    createSession,
    deleteSession,
    deleteSessionsBatch,
    refreshActive,
  } = useSessionStore();
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [batchDeleting, setBatchDeleting] = useState(false);
  const [statsSession, setStatsSession] = useState<SessionSummary | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [confirmBatchDelete, setConfirmBatchDelete] = useState(false);
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; sessionId: string; title: string } | null>(null);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  useEffect(() => {
    setSelectedIds(new Set());
  }, [sessions.length]);

  useEffect(() => {
    if (!contextMenu) return;
    const close = () => setContextMenu(null);
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, [contextMenu]);

  const handleOpen = async (id: string) => {
    await openSession(id);
    onOpenSession?.(id);
  };

  const handleDelete = async (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    setConfirmDeleteId(id);
  };

  const executeDelete = async () => {
    const id = confirmDeleteId;
    if (!id) return;
    setDeletingId(id);
    await deleteSession(id);
    setDeletingId(null);
    setConfirmDeleteId(null);
  };

  const toggleSelect = (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    toggleSelectById(id);
  };

  const toggleSelectById = (id: string) => {
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

  const handleBatchDeleteClick = useCallback(() => {
    if (selectedIds.size === 0) return;
    setConfirmBatchDelete(true);
  }, [selectedIds]);

  const executeBatchDelete = useCallback(async () => {
    setBatchDeleting(true);
    await deleteSessionsBatch(Array.from(selectedIds));
    setBatchDeleting(false);
    setConfirmBatchDelete(false);
  }, [selectedIds, deleteSessionsBatch]);

  const inBatchMode = selectedIds.size > 0;

  return (
    <aside className="sidebar">
      <div className="sidebar-head">
        <div className="sidebar-head-row">
          <div className="brand">
            <span className="brand-mark">GC</span>
            <span className="brand-name">Grace Code</span>
          </div>
          <button className="sidebar-collapse-btn" type="button" aria-label="Collapse sidebar" onClick={onToggleCollapse}>
            ‹
          </button>
        </div>

        <div className="sidebar-action-row">
          <button className="btn-primary sidebar-primary" type="button" onClick={async () => {
            try {
              const defaults = await getAppDefaults();
              createSession(defaults.default_agent || "build");
            } catch {
              createSession();  // fallback to build
            }
          }}>
            + New Session
          </button>
        </div>

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

        {storeError && (
          <div className="session-error-banner" role="alert" style={{ padding: 8, background: "var(--error)", color: "#fff", borderRadius: 6, margin: 8 }}>
            <span style={{ fontSize: 12 }}>{storeError}</span>
            <button onClick={() => loadSessions()} style={{ marginLeft: 8, fontSize: 11, background: "rgba(255,255,255,0.2)", border: "none", borderRadius: 3, color: "#fff", cursor: "pointer", padding: "2px 8px" }}>Retry</button>
          </div>
        )}
        <div id="session-list" className="session-list">
          {isLoading && sessions.length === 0 && <div className="empty-state">Loading…</div>}
          {!isLoading && sessions.length === 0 && <div className="empty-state">No sessions yet.</div>}

          {sessions.map((s) => (
            <div
              key={s.id}
              role="button"
              tabIndex={0}
              aria-current={s.id === activeId ? "true" : undefined}
              className={`session-item ${s.id === activeId ? "active" : ""}`}
              onClick={() => handleOpen(s.id)}
              onContextMenu={(e) => { e.preventDefault(); setContextMenu({ x: e.clientX, y: e.clientY, sessionId: s.id, title: s.title || s.id }); }}
              onKeyDown={(e: React.KeyboardEvent) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); handleOpen(s.id); } }}
            >
              <span className={`session-dot ${statusClass(s.status)}`} />
              <span className="session-title" title={s.title || s.summary || s.id}>
                {s.title || s.summary || s.id}
              </span>
              <span className="session-time">{formatRelative(s.updated_at)}</span>
              <span className="session-actions">
                <button
                  className="session-action-btn"
                  title="Rename"
                  aria-label="Rename session"
                  onClick={async (e) => {
                    e.stopPropagation();
                    const name = prompt("Rename session:", s.title || "");
                    if (name && name.trim() && name.trim() !== s.title) {
                      try {
                        await updateSession(s.id, { title: name.trim() });
                        loadSessions();
                        if (s.id === activeId) refreshActive();
                      } catch { /* ignore */ }
                    }
                  }}
                >✎</button>
                <button
                  className="session-action-btn session-action-delete"
                  title="Delete"
                  aria-label="Delete session"
                  disabled={deletingId === s.id}
                  onClick={(e) => handleDelete(e, s.id)}
                >{deletingId === s.id ? "…" : "×"}</button>
              </span>
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
            onClick={handleBatchDeleteClick}
            disabled={batchDeleting}
          >
            {batchDeleting ? "Deleting…" : `Delete ${selectedIds.size}`}
          </button>
        </div>
      )}

      {statsSession ? (
        <SessionStatsDrawer session={statsSession} onClose={() => setStatsSession(null)} />
      ) : null}

      <ConfirmModal
        open={!!confirmDeleteId}
        title="Delete session"
        message={`Permanently delete this session? This cannot be undone.`}
        confirmLabel="Delete"
        danger
        loading={deletingId === confirmDeleteId}
        onConfirm={executeDelete}
        onCancel={() => setConfirmDeleteId(null)}
      />

      <ConfirmModal
        open={confirmBatchDelete}
        title="Delete sessions"
        message={`Permanently delete ${selectedIds.size} session${selectedIds.size > 1 ? "s" : ""}? This cannot be undone.`}
        confirmLabel={`Delete ${selectedIds.size}`}
        danger
        loading={batchDeleting}
        onConfirm={executeBatchDelete}
        onCancel={() => setConfirmBatchDelete(false)}
      />

      {contextMenu && (
        <div className="ctx-menu" style={{ left: contextMenu.x, top: contextMenu.y }}>
          <button className="ctx-menu-item" onClick={async () => {
            const name = prompt("Rename:", contextMenu.title);
            if (name?.trim()) { try { await updateSession(contextMenu.sessionId, { title: name.trim() }); loadSessions(); if (contextMenu.sessionId === activeId) refreshActive(); } catch { /* ok */ } }
            setContextMenu(null);
          }}>✎ Rename</button>
          <button className="ctx-menu-item" onClick={() => { navigator.clipboard.writeText(contextMenu.sessionId).catch(() => {}); setContextMenu(null); }}>📋 Copy ID</button>
          <button className="ctx-menu-item ctx-menu-danger" onClick={() => { setConfirmDeleteId(contextMenu.sessionId); setContextMenu(null); }}>× Delete</button>
        </div>
      )}
    </aside>
  );
}
