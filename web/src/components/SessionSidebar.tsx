import { useEffect, useState } from "react";
import { useSessionStore } from "../stores/sessionStore";
import { useChatStore } from "../stores/chatStore";

export function SessionSidebar() {
  const { sessions, activeId, isLoading, loadSessions, openSession, createSession, deleteSession } =
    useSessionStore();
  const { clear } = useChatStore();
  const [deletingId, setDeletingId] = useState<string | null>(null);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  const handleOpen = async (id: string) => {
    clear(); // reset timeline + events
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

  return (
    <aside className="sidebar">
      <div className="sidebar-head">
        <div className="brand">
          <span className="brand-mark">GC</span>
          <span className="brand-name">Grace Code</span>
        </div>
        <button className="btn-primary" type="button" onClick={() => createSession()}>
          + New chat
        </button>
      </div>
      <div className="sidebar-section sidebar-sessions">
        <div className="sidebar-title">Sessions</div>
        <div id="session-list" className="session-list">
          {isLoading && sessions.length === 0 && (
            <div className="empty-state">Loading…</div>
          )}
          {!isLoading && sessions.length === 0 && (
            <div className="empty-state">No sessions yet.</div>
          )}
          {sessions.map((s) => (
            <div
              key={s.id}
              className={`session-item ${s.id === activeId ? "active" : ""}`}
              onClick={() => handleOpen(s.id)}
              style={{ position: "relative" }}
            >
              <div className="session-preview">
                {s.summary
                  ? s.summary.slice(0, 80)
                  : (s.title || s.id).slice(0, 30)}
              </div>
              <div className="session-meta">
                {s.agent_name} · {s.status}
                {s.message_count != null && ` · ${s.message_count} msgs`}
              </div>
              <button
                onClick={(e) => handleDelete(e, s.id)}
                style={{
                  position: "absolute", right: 4, top: 4,
                  background: "none", border: "none",
                  color: "var(--text-muted)", cursor: "pointer",
                  fontSize: 14, padding: "2px 6px", borderRadius: 4,
                  lineHeight: 1, display: activeId === s.id ? "block" : "none",
                }}
                title="Delete session"
                disabled={deletingId === s.id}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      </div>
    </aside>
  );
}
