import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { RunOutcomeBar } from "./RunOutcomeBar";


describe("RunOutcomeBar", () => {
  it("renders structured validation and workspace facts outside answer text", () => {
    const html = renderToStaticMarkup(
      <RunOutcomeBar
        outcome={{
          status: "completed",
          verification: {
            status: "unverified",
            reason: "not_run",
            checks: [],
          },
          workspaceDelta: {
            has_changes: true,
            changed_files: ["agent/task.py", "web/src/types/events.ts"],
            patch_available: true,
          },
          evidenceSummary: {
            total: 7,
            by_kind: { tool_call_completed: 3 },
            failed: 0,
          },
          runId: "run-1",
        }}
        steps={3}
        tokens={12400}
      />,
    );

    expect(html).toContain("Validation not run");
    expect(html).toContain("2 files changed");
    expect(html).toContain("7 evidence");
    expect(html).toContain("aria-expanded=\"false\"");
    expect(html).not.toContain("Code changes were made but NOT independently verified");
  });

  it("labels failed and cancelled runs without creating assistant prose", () => {
    const failed = renderToStaticMarkup(
      <RunOutcomeBar outcome={{ status: "failed", error: "model error" }} />,
    );
    const cancelled = renderToStaticMarkup(
      <RunOutcomeBar outcome={{ status: "cancelled" }} />,
    );

    expect(failed).toContain("Failed");
    expect(cancelled).toContain("Cancelled");
  });
});
