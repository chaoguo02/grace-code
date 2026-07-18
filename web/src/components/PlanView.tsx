import { useEffect } from "react";
import { useSessionStore } from "../stores/sessionStore";
import { useChatStore } from "../stores/chatStore";
import * as api from "../api/sessions";

export function PlanView() {
  const { activeId, activeDetail } = useSessionStore();
  const { planApproval, approvePlan, rejectPlan, isRunning, clear } = useChatStore();

  // Load the active session's details on mount
  useEffect(() => {
    if (activeId) {
      useSessionStore.getState().refreshActive();
    }
  }, [activeId]);

  const isPlanSession = activeDetail?.agent_name === "plan";
  const hasPlan = planApproval?.isWaiting;

  return (
    <section className="view active" data-view-name="plan" style={{ padding: 20, overflow: "auto" }}>
      <h2 style={{ margin: "0 0 16px", fontSize: 18 }}>Plan Mode</h2>

      {!activeId && (
        <div className="empty-state" style={{ padding: 40 }}>
          Select a session to view its plan.
        </div>
      )}

      {activeId && !hasPlan && !isPlanSession && (
        <div className="empty-state" style={{ padding: 40 }}>
          <p>No plan available for this session.</p>
          <p style={{ color: "var(--text-muted)", fontSize: 13 }}>
            Start a plan by creating a session with the <strong>plan</strong> agent
            or sending a message with <strong>analyze-first</strong> intent.
          </p>
          <button
            className="btn-primary"
            style={{ marginTop: 12 }}
            type="button"
            onClick={async () => {
              if (!activeId) return;
              clear();
              try {
                await api.chat(
                  activeId,
                  "Analyze the codebase and produce a structured implementation plan."
                );
              } catch {
                /* ignore */
              }
            }}
          >
            Start Plan Analysis
          </button>
        </div>
      )}

      {hasPlan && planApproval && (
        <div className="plan-card">
          <h2>📋 Plan Ready for Review</h2>
          <div
            style={{
              maxHeight: 400,
              overflow: "auto",
              whiteSpace: "pre-wrap",
              fontSize: 13,
              lineHeight: 1.7,
              color: "var(--text)",
              fontFamily: "var(--font-ui)",
            }}
          >
            {planApproval.planText}
          </div>
          <div className="plan-actions" style={{ marginTop: 16 }}>
            <button
              className="btn-approve"
              type="button"
              disabled={isRunning}
              onClick={() => approvePlan()}
            >
              ✓ Approve & Build
            </button>
            <button
              className="btn-reject"
              type="button"
              disabled={isRunning}
              onClick={() => rejectPlan("Please revise the plan with more detail.")}
            >
              ✗ Reject
            </button>
          </div>
        </div>
      )}

      {activeId && isPlanSession && !hasPlan && (
        <div className="empty-state" style={{ padding: 20 }}>
          <p style={{ color: "var(--text-dim)" }}>
            Session agent: <strong>{activeDetail?.agent_name}</strong>
            {" · "} status: <strong>{activeDetail?.status}</strong>
          </p>
          {activeDetail?.summary && (
            <div
              className="plan-card"
              style={{ marginTop: 12, maxHeight: 300, overflow: "auto" }}
            >
              <pre style={{ whiteSpace: "pre-wrap", fontSize: 13 }}>
                {activeDetail.summary}
              </pre>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
