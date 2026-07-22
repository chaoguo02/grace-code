import { useEffect, useState } from "react";
import { getSessionStats, getSessionSteps } from "../api/stats";
import { getSessionDiffs } from "../api/diffs";
import type { SessionSummary } from "../types";
import type { SessionDiff, StepLog, SessionStats as StatsType } from "../types/stats";

interface SessionStatsDrawerProps {
  session: SessionSummary | null;
  onClose: () => void;
}

function formatDateTime(value?: string | null) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString();
}

function formatDuration(ms?: number) {
  if (!ms || ms <= 0) return "0s";
  const totalSec = ms / 1000;
  if (totalSec < 60) return `${totalSec.toFixed(1)}s`;
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  return `${min}m ${sec.toFixed(1)}s`;
}

export function SessionStatsDrawer({ session, onClose }: SessionStatsDrawerProps) {
  const [stats, setStats] = useState<StatsType | null>(null);
  const [steps, setSteps] = useState<StepLog[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [diffs, setDiffs] = useState<SessionDiff[]>([]);

  useEffect(() => {
    let cancelled = false;
    if (!session) return;
    setLoading(true);
    setErr(null);
    Promise.all([
      getSessionStats(session.id).catch(() => null),
      getSessionSteps(session.id).catch(() => []),
      getSessionDiffs(session.id).catch(() => []),
    ]).then(([statsData, stepsData, diffsData]) => {
      if (cancelled) return;
      setStats(statsData);
      setSteps((stepsData || []) as StepLog[]);
      setDiffs((diffsData || []) as SessionDiff[]);
    }).catch(() => {
      if (!cancelled) setErr("Failed to load session stats");
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [session]);

  if (!session) return null;

  const groupedTools = steps.reduce<Record<string, StepLog[]>>((acc, step) => {
    const key = step.tool_name || "Tool";
    acc[key] = acc[key] || [];
    acc[key].push(step);
    return acc;
  }, {});

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <div className="session-drawer" onClick={(e) => e.stopPropagation()}>
        <div className="session-drawer-header">
          <div>
            <div className="summary-label">Session stats</div>
            <h3 className="session-drawer-title">{session.title || session.id}</h3>
            <div className="session-drawer-subtitle">{session.agent_name}</div>
          </div>
          <button className="drawer-close" type="button" onClick={onClose}>×</button>
        </div>

        {loading && <div className="session-drawer-loading">Loading stats…</div>}
        {err && <div className="session-drawer-error">⚠ {err}</div>}

        {!loading && !err && (<>
        <div className="session-drawer-grid">
          <div className="session-drawer-stat"><span>Status</span><strong>{stats?.status || session.status}</strong></div>
          <div className="session-drawer-stat"><span>Steps</span><strong>{stats?.total_steps ?? session.message_count ?? 0} / 10</strong></div>
          <div className="session-drawer-stat"><span>Tokens</span><strong>{(stats?.total_tokens ?? session.total_tokens_estimate ?? 0).toLocaleString()}</strong></div>
          <div className="session-drawer-stat"><span>Duration</span><strong>{formatDuration(stats?.total_duration_ms)}</strong></div>
          <div className="session-drawer-stat"><span>Created</span><strong>{formatDateTime(session.created_at)}</strong></div>
          <div className="session-drawer-stat"><span>Completed</span><strong>{formatDateTime(session.completed_at)}</strong></div>
        </div>

        <div className="session-drawer-section">
          <div className="session-drawer-section-title">Tool Calls</div>
          <div className="session-tool-table">
            {Object.entries(groupedTools).map(([tool, rows]) => {
              const successCount = rows.filter((row) => row.status === "success").length;
              const avgMs = rows.length ? rows.reduce((sum, row) => sum + (row.duration_ms || 0), 0) / rows.length : 0;
              return (
                <div key={tool} className="session-tool-row">
                  <span>{tool}</span>
                  <span>{rows.length} calls</span>
                  <span>✓ {rows.length ? Math.round((successCount / rows.length) * 100) : 0}%</span>
                  <span>⚡ {formatDuration(avgMs)}</span>
                </div>
              );
            })}
            {!Object.keys(groupedTools).length && <div className="empty-state">No step logs yet.</div>}
          </div>
        </div>

        <div className="session-drawer-section">
          <div className="session-drawer-section-title">Diffs</div>
          <div className="session-drawer-diffs">
            <span>{diffs.filter((d) => d.status === "pending").length} pending</span>
            <span>{diffs.length} total</span>
          </div>
        </div>
        </>)}
      </div>
    </div>
  );
}
