import { useEffect, useMemo, useState } from "react";
import { getDailyRollups, getToolRankings, getRecentSessionStats } from "../api/stats";
import { useSessionStore } from "../stores/sessionStore";
import type { DailyRollup, SessionStats } from "../types/stats";

function formatTokens(value?: number) {
  if (!value) return "0";
  if (value >= 1000) return `${(value / 1000).toFixed(value >= 10_000 ? 0 : 1)}K`;
  return String(value);
}

function formatDuration(ms?: number) {
  if (!ms || ms <= 0) return "0s";
  const totalSec = Math.round(ms / 1000);
  if (totalSec < 60) return `${totalSec}s`;
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  return `${min}m ${sec}s`;
}

function dayLabel(date: string) {
  const d = new Date(date);
  if (Number.isNaN(d.getTime())) return date;
  return `${String(d.getMonth() + 1).padStart(2, "0")}/${String(d.getDate()).padStart(2, "0")}`;
}

export function StatsDashboard() {
  const [daily, setDaily] = useState<DailyRollup[]>([]);
  const [toolRankings, setToolRankings] = useState<Record<string, number>>({});
  const [sessions, setSessions] = useState<SessionStats[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const activeId = useSessionStore((s) => s.activeId);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    Promise.all([getDailyRollups(30), getToolRankings(7), getRecentSessionStats(30)])
      .then(([dailyData, toolData, sessionData]) => {
        if (cancelled) return;
        setDaily(dailyData);
        setToolRankings(toolData);
        setSessions(sessionData);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Failed to load statistics");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [activeId]);

  const maxDailyTokens = useMemo(() => Math.max(1, ...daily.map((d) => d.total_tokens || 0)), [daily]);
  const toolEntries = useMemo(() => Object.entries(toolRankings).sort((a, b) => b[1] - a[1]).slice(0, 8), [toolRankings]);
  const maxToolCount = useMemo(() => Math.max(1, ...toolEntries.map(([, c]) => c)), [toolEntries]);

  const totalTokens = useMemo(() => daily.reduce((s, d) => s + (d.total_tokens || 0), 0), [daily]);
  const totalSessions = useMemo(() => daily.reduce((s, d) => s + (d.session_count || 0), 0), [daily]);
  const totalDuration = useMemo(() => daily.reduce((s, d) => s + (d.total_duration_ms || 0), 0), [daily]);

  return (
    <section className="view active" data-view-name="stats">
      <div className="plan-page stats-page">
        <div className="plan-hero stats-hero">
          <div>
            <div className="summary-label">Stats Workspace</div>
            <h2 className="plan-hero-title">Usage, throughput, and session health</h2>
            <p className="plan-hero-body">Token trends, tool rankings, and recent session performance.</p>
          </div>
          <div className="plan-hero-stats">
            <div className="meta-pill"><div className="meta-pill-label">30d Tokens</div><div className="meta-pill-value">{formatTokens(totalTokens)}</div></div>
            <div className="meta-pill"><div className="meta-pill-label">30d Sessions</div><div className="meta-pill-value">{totalSessions}</div></div>
            <div className="meta-pill"><div className="meta-pill-label">30d Runtime</div><div className="meta-pill-value">{formatDuration(totalDuration)}</div></div>
          </div>
        </div>

        {error && (
          <div style={{ padding: 12, background: "var(--error)", color: "#fff", borderRadius: 6, margin: "0 20px 12px", display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ fontSize: 13 }}>{error}</span>
            <button onClick={() => { setLoading(true); setError(null); Promise.all([getDailyRollups(30), getToolRankings(7), getRecentSessionStats(30)]).then(([a,b,c]) => { setDaily(a); setToolRankings(b); setSessions(c); }).catch((e: unknown) => setError(e instanceof Error ? e.message : "Failed")).finally(() => setLoading(false)); }} style={{ marginLeft: "auto", fontSize: 12, background: "rgba(255,255,255,0.2)", border: "none", borderRadius: 3, color: "#fff", cursor: "pointer", padding: "4px 10px" }}>Retry</button>
          </div>
        )}

        <div className="stats-grid">
          {/* Token bar chart */}
          <div className="stats-card">
            <div className="stats-card-header">
              <div><div className="summary-label">Tokens by day</div><h3 className="stats-card-title">Last 30 days</h3></div>
            </div>
            <div className="stats-bar-chart">
              {loading && <div className="empty-state">Loading...</div>}
              {!loading && daily.length === 0 && <div className="empty-state">No daily rollups yet.</div>}
              {daily.map((item) => (
                <div key={item.date} className="stats-bar-item">
                  <div className="stats-bar" style={{ height: `${Math.max(10, Math.round(((item.total_tokens || 0) / maxDailyTokens) * 120))}px` }} title={`${dayLabel(item.date)}: ${item.total_tokens.toLocaleString()} tok`} />
                  <div className="stats-bar-value">{formatTokens(item.total_tokens)}</div>
                  <div className="stats-bar-label">{dayLabel(item.date)}</div>
                </div>
              ))}
            </div>
          </div>

          {/* Tool rankings */}
          <div className="stats-card">
            <div className="stats-card-header">
              <div><div className="summary-label">Tool rankings</div><h3 className="stats-card-title">Last 7 days</h3></div>
            </div>
            <div className="stats-ranking-list">
              {loading && <div className="empty-state">Loading...</div>}
              {!loading && toolEntries.length === 0 && <div className="empty-state">No tool activity yet.</div>}
              {toolEntries.map(([tool, count]) => (
                <div key={tool} className="stats-ranking-row">
                  <span className="stats-ranking-name">{tool}</span>
                  <div className="stats-ranking-bar-wrap">
                    <div className="stats-ranking-bar" style={{ width: `${Math.max(12, Math.round((count / maxToolCount) * 100))}%` }} />
                  </div>
                  <strong>{count}</strong>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Recent sessions */}
        <div className="stats-card stats-card-wide">
          <div className="stats-card-header">
            <div><div className="summary-label">Recent sessions</div><h3 className="stats-card-title">Execution summaries</h3></div>
          </div>
          <div className="stats-session-list">
            {loading && <div className="empty-state">Loading...</div>}
            {!loading && sessions.length === 0 && <div className="empty-state">No recent sessions.</div>}
            {sessions.slice(0, 20).map((session) => (
              <div key={session.session_id} className="stats-session-row">
                <div className="stats-session-main">
                  <strong>{session.agent_name}</strong>
                  <span>{session.status}</span>
                  <span>{session.total_steps ?? 0} steps</span>
                  <span>{formatTokens(session.total_tokens)} tok</span>
                  <span>{formatDuration(session.total_duration_ms)}</span>
                </div>
                <div className="stats-session-subtle">{session.session_id}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}
