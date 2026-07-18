import { useEffect } from "react";
import { useSessionStore } from "../stores/sessionStore";
import { useChatStore } from "../stores/chatStore";
import * as api from "../api/sessions";

function PlanEmptyState({
  title,
  body,
  action,
}: {
  title: string;
  body: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="plan-empty">
      <div className="plan-empty-icon">◇</div>
      <div className="plan-empty-title">{title}</div>
      <div className="plan-empty-body">{body}</div>
      {action}
    </div>
  );
}

export function PlanView() {
  const { activeId, activeDetail } = useSessionStore();
  const { planApproval, approvePlan, rejectPlan, isRunning, clear } = useChatStore();

  useEffect(() => {
    if (activeId) {
      useSessionStore.getState().refreshActive();
    }
  }, [activeId]);

  const isPlanSession = activeDetail?.agent_name === "plan";
  const hasPlan = planApproval?.isWaiting;

  return (
    <section className="view active" data-view-name="plan">
      <div className="plan-page">
        <div className="plan-hero">
          <div>
            <div className="summary-label">Plan Workspace</div>
            <h2 className="plan-hero-title">Review before execution</h2>
            <p className="plan-hero-body">
              Use this space to inspect a generated plan, approve it into build execution,
              or send it back for revision.
            </p>
          </div>
          <div className="plan-hero-stats">
            <div className="meta-pill">
              <div className="meta-pill-label">Agent</div>
              <div className="meta-pill-value">{activeDetail?.agent_name || "—"}</div>
            </div>
            <div className="meta-pill">
              <div className="meta-pill-label">Status</div>
              <div className="meta-pill-value">{activeDetail?.status || "idle"}</div>
            </div>
          </div>
        </div>

        {!activeId && (
          <PlanEmptyState
            title="No active session"
            body="Select a session from the sidebar, or create a new one, to start using the planning workflow."
          />
        )}

        {activeId && !hasPlan && !isPlanSession && (
          <PlanEmptyState
            title="No plan has been generated yet"
            body="You can trigger a planning pass for this session. Grace Code will analyze the task, propose a structured plan, and pause here for approval."
            action={
              <button
                className="btn-primary"
                type="button"
                onClick={async () => {
                  if (!activeId) return;
                  clear();
                  try {
                    await api.chat(
                      activeId,
                      "Analyze the codebase and produce a structured implementation plan.",
                    );
                  } catch {
                    /* ignore */
                  }
                }}
              >
                Start Plan Analysis
              </button>
            }
          />
        )}

        {hasPlan && planApproval && (
          <div className="plan-card plan-card-prominent">
            <div className="plan-card-header">
              <div>
                <div className="summary-label">Plan Ready</div>
                <h3 className="plan-card-title">Structured execution proposal</h3>
              </div>
              <span className="trace-pill">Waiting for approval</span>
            </div>

            <div className="plan-scroll">
              <pre className="plan-pre">{planApproval.planText}</pre>
            </div>

            <div className="plan-card-footer">
              <div className="summary-subtle">
                Approve to continue into build execution, or reject to request a revised plan.
              </div>
              <div className="plan-actions">
                <button
                  className="btn-approve"
                  type="button"
                  disabled={isRunning}
                  onClick={() => approvePlan()}
                >
                  Approve & Build
                </button>
                <button
                  className="btn-reject"
                  type="button"
                  disabled={isRunning}
                  onClick={() => rejectPlan("Please revise the plan with more detail.")}
                >
                  Reject
                </button>
              </div>
            </div>
          </div>
        )}

        {activeId && isPlanSession && !hasPlan && (
          <div className="plan-card">
            <div className="plan-card-header">
              <div>
                <div className="summary-label">Plan Session</div>
                <h3 className="plan-card-title">Current planning state</h3>
              </div>
              <span className="trace-pill">{activeDetail?.status || "idle"}</span>
            </div>

            {activeDetail?.summary ? (
              <div className="plan-scroll">
                <pre className="plan-pre">{activeDetail.summary}</pre>
              </div>
            ) : (
              <PlanEmptyState
                title="This plan session has not produced output yet"
                body="Once the plan agent generates a structured result, it will appear here for review."
              />
            )}
          </div>
        )}
      </div>
    </section>
  );
}
