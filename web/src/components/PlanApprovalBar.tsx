import { useCallback, useEffect, useState } from "react";
import type { PlanApproval } from "../stores/chatStore";

interface PlanApprovalBarProps {
  approval: PlanApproval;
  feedback: string;
  onFeedbackChange: (value: string) => void;
  onApprove: (feedback: string) => void;
  onReject: (feedback: string) => void;
  onSave: () => void;
  onDiscard: () => void;
  disabled?: boolean;
}

function contractGoal(contract?: Record<string, unknown> | null): string {
  const goal = contract?.goal;
  return goal == null ? "" : String(goal).trim();
}

export function PlanApprovalBar({
  approval,
  feedback,
  onFeedbackChange,
  onApprove,
  onReject,
  onSave,
  onDiscard,
  disabled = false,
}: PlanApprovalBarProps) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const saved = approval.lifecycle === "saved";
  const revision = approval.revision ?? 1;
  const maxRevisions = approval.maxRevisions ?? 5;
  const finalRevision = revision >= maxRevisions;
  const goal = contractGoal(approval.contract);

  const approve = useCallback(
    () => onApprove(feedback.trim()),
    [feedback, onApprove],
  );
  const reject = useCallback(
    () => onReject(feedback.trim() || "Please revise the plan"),
    [feedback, onReject],
  );

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (disabled || event.ctrlKey || event.metaKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase();
      if (
        tag === "input" ||
        tag === "textarea" ||
        tag === "select" ||
        target?.getAttribute("contenteditable") === "true"
      ) {
        return;
      }

      if (event.key === "y" || event.key === "Y") {
        event.preventDefault();
        approve();
      } else if (event.key === "n" || event.key === "N") {
        event.preventDefault();
        reject();
      } else if ((event.key === "s" || event.key === "S") && !saved) {
        event.preventDefault();
        onSave();
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [approve, disabled, onSave, reject, saved]);

  return (
    <section
      className={`hitl-bar plan-hitl-bar${saved ? " is-saved" : ""}`}
      aria-label="Plan approval"
      aria-live="polite"
    >
      <div className="hitl-main-row plan-hitl-main-row">
        <span className="hitl-icon plan-hitl-icon" aria-hidden="true">
          {saved ? "✓" : "◆"}
        </span>
        <span className="hitl-tool-name">{saved ? "Plan saved" : "Plan ready"}</span>
        <span className="plan-hitl-revision">
          Revision {revision}/{maxRevisions}
          {finalRevision ? " · final" : ""}
        </span>
        {goal ? (
          <button
            className="plan-hitl-goal"
            type="button"
            onClick={() => setDetailsOpen((open) => !open)}
            aria-expanded={detailsOpen}
            title={goal}
          >
            {goal}
          </button>
        ) : null}

        <span className="hitl-spacer" />

        <input
          className="plan-hitl-feedback"
          value={feedback}
          onChange={(event) => onFeedbackChange(event.target.value)}
          placeholder="Optional feedback"
          aria-label="Plan review feedback"
          disabled={disabled}
        />
        <button
          className="hitl-btn plan-hitl-secondary"
          type="button"
          disabled={disabled}
          onClick={onDiscard}
        >
          Discard
        </button>
        <button
          className="hitl-btn plan-hitl-secondary"
          type="button"
          disabled={disabled || saved}
          onClick={onSave}
          title={saved ? "This plan is already saved" : "Save without building"}
        >
          <kbd>S</kbd> {saved ? "Saved" : "Save"}
        </button>
        <button
          className="hitl-btn hitl-deny"
          type="button"
          disabled={disabled}
          onClick={reject}
        >
          <kbd>N</kbd> Reject
        </button>
        <button
          className="hitl-btn hitl-approve"
          type="button"
          disabled={disabled}
          onClick={approve}
        >
          <kbd>Y</kbd> Approve &amp; Build
        </button>
      </div>

      {detailsOpen && goal ? (
        <div className="hitl-reason plan-hitl-details">
          <strong>Goal:</strong> {goal}
        </div>
      ) : null}
      {saved ? (
        <div className="hitl-reason">Saved for later · build has not started.</div>
      ) : null}
    </section>
  );
}
