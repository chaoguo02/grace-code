import { useEffect, useState, useMemo } from "react";
import { useSessionStore } from "../stores/sessionStore";
import { selectSessionUi, useChatStore } from "../stores/chatStore";
import { getSessionPlan } from "../api/sessions";
import { MarkdownRenderer } from "./MarkdownRenderer";

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

/** Extract structured goals (bullet list / checklist) from plan text. */
function extractGoals(planText: string): string[] {
  const lines = planText.split("\n");
  const goals: string[] = [];
  for (const line of lines) {
    const trimmed = line.trim();
    // Match "- [ ] text", "- [x] text", "- text", "* text", "1. text"
    const match = trimmed.match(/^[-*]\s*(?:\[.\]\s*)?(.+)$/) || trimmed.match(/^\d+\.\s*(.+)$/);
    if (match) {
      goals.push(match[1].trim());
    }
  }
  return goals.slice(0, 12); // cap at 12 to avoid overwhelming
}

/** Count markdown headings in plan text as a rough step estimate. */
function countPlanSections(planText: string): number {
  const headingMatches = planText.match(/^#{1,3}\s+/gm);
  return headingMatches ? headingMatches.length : 0;
}

export function PlanView() {
  const { activeId, activeDetail } = useSessionStore();
  const { planApproval, isRunning, steps, tokens } = useChatStore((s) =>
    selectSessionUi(s, activeId),
  );
  const { approvePlan, rejectPlan, savePlan, abortPlan } = useChatStore();
  const [planFile, setPlanFile] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (activeId) {
      getSessionPlan(activeId)
        .then((plan) => {
          if (!cancelled) setPlanFile(plan.has_plan ? plan.content : null);
        })
        .catch(() => { if (!cancelled) setPlanFile(null); });
    } else {
      setPlanFile(null);
    }
    return () => { cancelled = true; };
  }, [activeId]);

  const planText = planFile || planApproval?.planText || activeDetail?.summary || "";
  const isPlanSession = activeDetail?.agent_name === "plan";
  const hasPlan = !!planApproval?.isWaiting;
  const isCompleted = !hasPlan && isPlanSession && activeDetail?.status === "completed" && !!activeDetail?.summary;

  const goals = useMemo(() => extractGoals(planText), [planText]);
  const sectionCount = useMemo(() => countPlanSections(planText), [planText]);
  const revision = planApproval?.revision ?? 0;
  const maxRevisions = planApproval?.maxRevisions ?? 5;

  const showPlanCard = hasPlan || isCompleted;

  return (
    <section className="view active" data-view-name="plan">
      <div className="plan-page">
        {/* Hero header */}
        <div className="plan-hero">
          <div>
            <div className="summary-label">Plan Workspace</div>
            <h2 className="plan-hero-title">Review before execution</h2>
            <p className="plan-hero-body">
              Inspect a generated plan, approve it into build execution, or send it back for revision.
            </p>
          </div>
          <div className="plan-hero-stats">
            <div className="meta-pill">
              <div className="meta-pill-label">Agent</div>
              <div className="meta-pill-value">{activeDetail?.agent_name || "—"}</div>
            </div>
            <div className="meta-pill">
              <div className="meta-pill-label">Status</div>
              <div className="meta-pill-value">
                {hasPlan ? "reviewing" : activeDetail?.status || "idle"}
              </div>
            </div>
            {sectionCount > 0 && (
              <div className="meta-pill">
                <div className="meta-pill-label">Sections</div>
                <div className="meta-pill-value">{sectionCount}</div>
              </div>
            )}
          </div>
        </div>

        {/* No active session */}
        {!activeId && (
          <PlanEmptyState
            title="No active session"
            body="Select a session from the sidebar, or create a new one, to start using the planning workflow."
          />
        )}

        {/* No plan, not a plan session */}
        {activeId && !showPlanCard && !isPlanSession && (
          <PlanEmptyState
            title="No plan has been generated yet"
            body={
              isRunning
                ? "A plan analysis is already in progress. Check the Chat view for live progress."
                : "You can trigger a planning pass for this session. Grace Code will analyze the task, propose a structured plan, and pause here for approval."
            }
            action={
              <button
                className="btn-primary"
                type="button"
                disabled={isRunning}
                onClick={async () => {
                  if (!activeId || isRunning) return;
                  try {
                    const { sendChat } = useChatStore.getState();
                    await sendChat(
                      activeId,
                      "Analyze the codebase and produce a structured implementation plan.",
                      "analysis",
                    );
                  } catch {
                    /* ignore */
                  }
                }}
              >
                {isRunning ? "Analysis Running..." : "Start Plan Analysis"}
              </button>
            }
          />
        )}

        {/* Plan session with no output */}
        {activeId && isPlanSession && !showPlanCard && (
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
                <MarkdownRenderer className="plan-pre" content={activeDetail.summary} />
              </div>
            ) : (
              <PlanEmptyState
                title="This plan session has not produced output yet"
                body="Once the plan agent generates a structured result, it will appear here for review."
              />
            )}
          </div>
        )}

        {/* Plan card (ready for approval, or completed) */}
        {showPlanCard && (
          <div className="plan-card plan-card-prominent">
            {/* Stats bar */}
            <div className="plan-stats-bar">
              <div className="plan-stat">
                <span className="plan-stat-label">Status</span>
                <span className={`plan-stat-value ${hasPlan ? "plan-stat-waiting" : "plan-stat-done"}`}>
                  {hasPlan ? "⏳ Awaiting approval" : "✓ Completed"}
                </span>
              </div>
              {hasPlan && (
                <div className="plan-stat">
                  <span className="plan-stat-label">Revision</span>
                  <span className="plan-stat-value">
                    {revision} / {maxRevisions}
                    {revision >= maxRevisions && " (final)"}
                  </span>
                </div>
              )}
              <div className="plan-stat">
                <span className="plan-stat-label">Sections</span>
                <span className="plan-stat-value">{sectionCount || "—"}</span>
              </div>
              <div className="plan-stat">
                <span className="plan-stat-label">Goals</span>
                <span className="plan-stat-value">{goals.length || "—"}</span>
              </div>
              {(steps > 0 || tokens > 0) && (
                <div className="plan-stat">
                  <span className="plan-stat-label">Runtime</span>
                  <span className="plan-stat-value">
                    {steps > 0 ? `${steps} steps` : ""}
                    {tokens > 0 ? ` · ${tokens.toLocaleString()} tok` : ""}
                  </span>
                </div>
              )}
            </div>

            {/* Two-column: plan text + contract sidebar */}
            <div className="plan-content-grid">
              {/* Left: plan markdown */}
              <div className="plan-content-main">
                <div className="plan-card-header">
                  <div>
                    <div className="summary-label">
                      {hasPlan ? "Plan Ready" : "Plan Completed"}
                    </div>
                    <h3 className="plan-card-title">
                      {hasPlan ? "Structured execution proposal" : "Generated Plan"}
                    </h3>
                  </div>
                  <span className="trace-pill">{hasPlan ? "waiting" : "completed"}</span>
                </div>
                <div className="plan-scroll">
                  <MarkdownRenderer className="plan-pre" content={planText} />
                </div>
              </div>

              {/* Right: contract goals + actions */}
              <div className="plan-content-sidebar">
                {/* Contract / goals summary */}
                {goals.length > 0 && (
                  <div className="plan-goals-card">
                    <div className="plan-goals-title">Plan Outline</div>
                    <ol className="plan-goals-list">
                      {goals.map((goal, i) => (
                        <li key={i} className="plan-goal-item">{goal}</li>
                      ))}
                    </ol>
                  </div>
                )}

                {(() => {
                  const contractGoal = planApproval?.contract?.goal;
                  return contractGoal != null && goals.length === 0 && (
                    <div className="plan-goals-card">
                      <div className="plan-goals-title">Contract Goal</div>
                      <div className="plan-contract-goal">
                        {String(contractGoal)}
                      </div>
                    </div>
                  );
                })()}

                {/* Source indicator */}
                <div className="plan-source-hint">
                  {planFile
                    ? "Plan file loaded from .grace/plans/. Approve to execute."
                    : hasPlan
                      ? "Approve to continue into build, or reject to request a revised plan."
                      : "This plan was generated previously. Approve to execute it."}
                </div>

                {/* Actions */}
                <div className="plan-sidebar-actions">
                  {hasPlan && (
                    <>
                      <button
                        className="btn-approve plan-action-btn"
                        type="button"
                        disabled={isRunning}
                        onClick={() => approvePlan(activeId)}
                      >
                        Approve &amp; Build
                      </button>
                      <button
                        className="btn-secondary plan-action-btn"
                        type="button"
                        disabled={isRunning}
                        onClick={() => savePlan(activeId)}
                      >
                        Save Plan
                      </button>
                      <button
                        className="btn-reject plan-action-btn"
                        type="button"
                        disabled={isRunning}
                        onClick={() => rejectPlan(activeId, "Please revise the plan with more detail.")}
                      >
                        Request Revision
                      </button>
                      <button
                        className="btn-danger plan-action-btn"
                        type="button"
                        disabled={isRunning}
                        onClick={() => abortPlan(activeId)}
                      >
                        Discard
                      </button>
                    </>
                  )}
                  {!hasPlan && (
                    <>
                      <button
                        className="btn-approve plan-action-btn"
                        type="button"
                        disabled={isRunning}
                        onClick={() => approvePlan(activeId)}
                      >
                        Approve &amp; Build
                      </button>
                      <button
                        className="btn-secondary plan-action-btn"
                        type="button"
                        disabled={isRunning}
                        onClick={() => savePlan(activeId)}
                      >
                        Save
                      </button>
                      <button
                        className="btn-danger plan-action-btn"
                        type="button"
                        disabled={isRunning}
                        onClick={() => abortPlan(activeId)}
                      >
                        Discard
                      </button>
                    </>
                  )}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
