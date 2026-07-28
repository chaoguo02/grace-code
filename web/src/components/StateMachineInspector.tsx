/**
 * StateMachineInspector — TSM state flow viewer for the active session.
 *
 * Derives state from session status + trace events without a dedicated API.
 */
import { useMemo } from "react";
import { useSessionStore } from "../stores/sessionStore";
import { selectSessionUi, useChatStore } from "../stores/chatStore";

interface GuardInfo {
  name: string;
  triggered: boolean;
  detail?: string;
}

interface StateNode {
  label: string;
  key: string;
  active: boolean;
  past: boolean;
}

const STATES: StateNode[] = [
  { label: "Pending", key: "queued", active: false, past: false },
  { label: "Running", key: "running", active: false, past: false },
  { label: "Completing", key: "completing", active: false, past: false },
  { label: "Completed", key: "completed", active: false, past: false },
  { label: "Failed", key: "failed", active: false, past: false },
  { label: "Cancelled", key: "cancelled", active: false, past: false },
];

export function StateMachineInspector() {
  const activeId = useSessionStore((s) => s.activeId);
  const activeDetail = useSessionStore((s) => s.activeDetail);
  const { isRunning } = useChatStore((s) => selectSessionUi(s, activeId));

  const status = activeDetail?.status || "idle";
  const currentState = isRunning ? "running" : status;

  const stateNodes = useMemo(() =>
    STATES.map((s) => ({
      ...s,
      active: s.key === currentState,
      past: s.key === "queued" || (s.key === "running" && !isRunning && status === "completed"),
    })),
    [currentState, isRunning, status],
  );

  // Derive guards from session data
  const guards = useMemo((): GuardInfo[] => {
    const list: GuardInfo[] = [];
    if (!activeDetail) return list;

    const meta = activeDetail.metadata as Record<string, unknown> | undefined;

    // Circuit breaker guard
    list.push({
      name: "circuit_breaker",
      triggered: !!meta?.circuit_tripped,
      detail: meta?.circuit_tripped ? "Max consecutive tool errors reached" : undefined,
    });

    // Consecutive failures guard
    list.push({
      name: "consecutive_failures",
      triggered: false,
      detail: "Registered during initialization",
    });

    // Git diff guard (only relevant for write sessions)
    if (activeDetail.agent_name !== "explore") {
      list.push({
        name: "git_diff_guard",
        triggered: false,
        detail: "Requires workspace changes before completion",
      });
    }

    // Budget guard
    list.push({
      name: "budget_exhausted",
      triggered: status === "failed" || status === "max_steps",
      detail: status === "max_steps" ? "Max steps reached" : undefined,
    });

    return list;
  }, [activeDetail, status]);

  if (!activeId) return null;

  const terminalStates = ["completed", "failed", "cancelled"];

  return (
    <div style={{ padding: 16 }}>
      <div className="summary-label" style={{ marginBottom: 8 }}>Task State Machine</div>

      {/* State flow */}
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap", marginBottom: 16 }}>
        {stateNodes.map((node, i) => (
          <div key={node.key} style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{
              padding: "4px 10px",
              borderRadius: 4,
              fontSize: 11,
              fontWeight: 600,
              background: node.active ? "var(--accent)" : node.past ? "var(--green, #4caf50)" : "var(--border)",
              color: node.active || node.past ? "#fff" : "var(--text-dim)",
            }}>
              {node.label}
            </span>
            {i < stateNodes.length - 1 && (
              <span style={{ color: "var(--text-dim)", fontSize: 10 }}>→</span>
            )}
          </div>
        ))}
      </div>

      {/* Current state indicator */}
      <div style={{
        padding: "8px 12px", borderRadius: 6, marginBottom: 12,
        background: terminalStates.includes(currentState) ? "var(--bg-elev)" : "var(--accent-soft)",
        border: "1px solid " + (terminalStates.includes(currentState) ? "var(--border)" : "var(--accent)"),
        fontSize: 12,
      }}>
        <strong>Current:</strong> {currentState}
        {isRunning && <span style={{ marginLeft: 8, color: "var(--accent)" }}>◌ agent loop active</span>}
        {!isRunning && terminalStates.includes(currentState) && (
          <span style={{ marginLeft: 8, color: "var(--green, #4caf50)" }}>✓ terminal</span>
        )}
      </div>

      {/* Guards */}
      <div className="summary-label" style={{ marginBottom: 6 }}>Registered Guards ({guards.length})</div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {guards.map((g) => (
          <div key={g.name} style={{
            padding: "6px 10px", borderRadius: 4, fontSize: 11,
            background: g.triggered ? "var(--error)" : "var(--bg-elev)",
            color: g.triggered ? "#fff" : "var(--text)",
            border: "1px solid " + (g.triggered ? "var(--error)" : "var(--border)"),
            display: "flex", justifyContent: "space-between",
          }}>
            <span style={{ fontFamily: "var(--font-mono)", fontWeight: 600 }}>{g.name}</span>
            <span style={{ color: g.triggered ? "rgba(255,255,255,0.8)" : "var(--text-dim)" }}>
              {g.triggered ? "⚠ triggered" : "active"}
            </span>
          </div>
        ))}
      </div>
      {guards.some(g => g.detail) && (
        <div style={{ marginTop: 6, fontSize: 10, color: "var(--text-muted)" }}>
          {guards.filter(g => g.detail).map(g => (
            <div key={g.name}>• {g.name}: {g.detail}</div>
          ))}
        </div>
      )}
    </div>
  );
}
