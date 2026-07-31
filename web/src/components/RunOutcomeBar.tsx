import { useState } from "react";
import type { RunOutcome } from "../types/blocks";

interface RunOutcomeBarProps {
  outcome?: RunOutcome;
  steps?: number;
  tokens?: number;
  onInspect?: () => void;
}

const STATUS_LABELS: Record<RunOutcome["status"], string> = {
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
  partial: "Partial",
  gave_up: "Gave up",
  blocked: "Blocked",
};

function verificationLabel(outcome: RunOutcome): string {
  const status = outcome.verification?.status;
  const reason = outcome.verification?.reason;
  if (status === "verified") return "Validation passed";
  if (status === "failed") return "Validation failed";
  if (status === "unverified" && reason === "not_run") return "Validation not run";
  if (status === "unavailable") return "Validation unavailable";
  if (status === "not_applicable") return "Validation not applicable";
  return status ? `Validation: ${status.replace(/_/g, " ")}` : "Validation unavailable";
}

export function RunOutcomeBar({
  outcome,
  steps = 0,
  tokens = 0,
  onInspect,
}: RunOutcomeBarProps) {
  const [expanded, setExpanded] = useState(false);
  if (!outcome) return null;

  const verification = outcome.verification;
  const workspace = outcome.workspaceDelta;
  const changedFiles = workspace?.changed_files ?? [];
  const hasDetails = Boolean(
    outcome.error ||
    outcome.terminationReason && outcome.terminationReason !== "none" ||
    verification?.checks?.length ||
    changedFiles.length ||
    outcome.evidenceSummary?.total ||
    outcome.runId,
  );

  return (
    <section className={`run-outcome run-outcome-${outcome.status}`}>
      <button
        type="button"
        className="run-outcome-summary"
        aria-expanded={expanded}
        disabled={!hasDetails}
        onClick={() => hasDetails && setExpanded((value) => !value)}
      >
        <span className="run-outcome-status-mark" aria-hidden="true">
          {outcome.status === "completed" ? "✓" : outcome.status === "failed" ? "!" : "–"}
        </span>
        <span>{STATUS_LABELS[outcome.status]}</span>
        <span className="run-outcome-separator">·</span>
        <span>{verificationLabel(outcome)}</span>
        {changedFiles.length > 0 && (
          <>
            <span className="run-outcome-separator">·</span>
            <span>{changedFiles.length} file{changedFiles.length === 1 ? "" : "s"} changed</span>
          </>
        )}
        {(outcome.evidenceSummary?.total || 0) > 0 && (
          <>
            <span className="run-outcome-separator">·</span>
            <span>{outcome.evidenceSummary?.total} evidence</span>
          </>
        )}
        {steps > 0 && <span className="run-outcome-metric">{steps} steps</span>}
        {tokens > 0 && <span className="run-outcome-metric">{(tokens / 1000).toFixed(1)}K tokens</span>}
        {hasDetails && <span className="run-outcome-chevron" aria-hidden="true">{expanded ? "⌃" : "⌄"}</span>}
      </button>
      {onInspect && (
        <button
          type="button"
          className="run-outcome-inspect"
          onClick={onInspect}
        >
          Inspect run
        </button>
      )}

      {expanded && (
        <div className="run-outcome-details">
          {outcome.terminationReason && outcome.terminationReason !== "none" && (
            <div><strong>Termination</strong><span>{outcome.terminationReason}</span></div>
          )}
          {outcome.error && (
            <div><strong>Error</strong><span className="run-outcome-error">{outcome.error}</span></div>
          )}
          {verification?.reason && verification.reason !== "none" && (
            <div><strong>Validation reason</strong><span>{verification.reason.replace(/_/g, " ")}</span></div>
          )}
          {(verification?.checks ?? []).map((check, index) => (
            <div key={`${check.name}-${index}`}>
              <strong>{check.name}</strong>
              <span>{check.status}{check.command ? ` · ${check.command}` : ""}</span>
            </div>
          ))}
          {changedFiles.length > 0 && (
            <div>
              <strong>Changed files</strong>
              <span className="run-outcome-paths">{changedFiles.join("\n")}</span>
            </div>
          )}
          {(outcome.evidenceSummary?.total || 0) > 0 && (
            <div>
              <strong>Evidence</strong>
              <span>
                {outcome.evidenceSummary?.total} records
                {outcome.evidenceSummary?.failed
                  ? ` · ${outcome.evidenceSummary.failed} failed`
                  : ""}
              </span>
            </div>
          )}
          {outcome.runId && <div><strong>Run ID</strong><span>{outcome.runId}</span></div>}
        </div>
      )}
    </section>
  );
}
