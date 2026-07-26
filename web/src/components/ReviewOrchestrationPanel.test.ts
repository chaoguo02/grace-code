import { describe, expect, it } from "vitest";
import type { ReviewJob } from "../api/reviews";
import { deriveReviewOrchestration } from "./ReviewOrchestrationPanel";

function makeJob(overrides: Partial<ReviewJob> = {}): ReviewJob {
  return {
    id: "review-1",
    session_id: "session-1",
    status: "completed",
    workspace_revision: "workspace-revision",
    head_commit: "head-commit",
    retry_of: "",
    snapshot_available: true,
    diff_hash: "diff-hash",
    changed_files: ["src/example.ts"],
    focus: "",
    result: {
      finding_count: 2,
      invalid_finding_count: 1,
      findings: [
        {
          severity: "HIGH",
          category: "bug",
          title: "Race",
          description: "A reproducible race",
          evidence_status: "verified",
          corroboration_count: 2,
        },
        {
          severity: "LOW",
          category: "improvement",
          title: "Contract gap",
          description: "A missing contract assertion",
          evidence_status: "verified",
          corroboration_count: 1,
        },
      ],
    },
    error: "",
    tasks: [
      {
        id: "task-1",
        lens: "correctness",
        title: "Correctness",
        status: "completed",
        child_session_id: "child-1",
        result: {},
        error: "",
        attempts: [],
      },
      {
        id: "task-2",
        lens: "tests_contracts",
        title: "Contracts",
        status: "completed",
        child_session_id: "child-2",
        result: {},
        error: "",
        attempts: [],
      },
    ],
    created_at: "2026-07-26T00:00:00Z",
    updated_at: "2026-07-26T00:01:00Z",
    completed_at: "2026-07-26T00:01:00Z",
    ...overrides,
  };
}

describe("deriveReviewOrchestration", () => {
  it("derives terminal stages and evidence aggregation", () => {
    const model = deriveReviewOrchestration(makeJob());

    expect(model.stages.map((stage) => stage.state)).toEqual([
      "complete",
      "complete",
      "complete",
      "complete",
      "complete",
    ]);
    expect(model.severity).toEqual({ HIGH: 1, MEDIUM: 0, LOW: 1 });
    expect(model.verifiedFindings).toBe(2);
    expect(model.corroboratedFindings).toBe(1);
  });

  it("marks an incomplete reviewer and partial result as warnings", () => {
    const job = makeJob({
      status: "partial",
      tasks: [
        {
          id: "task-1",
          lens: "correctness",
          title: "Correctness",
          status: "failed",
          child_session_id: "child-1",
          result: {},
          error: "Budget exhausted",
          attempts: [],
        },
      ],
    });
    const model = deriveReviewOrchestration(job);

    expect(model.affectedTasks).toBe(1);
    expect(model.stages.find((stage) => stage.id === "parallel")?.state).toBe(
      "warning",
    );
    expect(model.stages.find((stage) => stage.id === "result")?.state).toBe(
      "warning",
    );
  });

  it("keeps aggregation active until the result is terminal", () => {
    const model = deriveReviewOrchestration(
      makeJob({ status: "aggregating", completed_at: null }),
    );

    expect(model.stages.find((stage) => stage.id === "aggregate")?.state).toBe(
      "active",
    );
    expect(model.stages.find((stage) => stage.id === "result")?.state).toBe(
      "pending",
    );
  });
});
