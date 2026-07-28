import { describe, expect, it } from "vitest";
import type { ContextSnapshotStats } from "../types/stats";
import { deriveContextComposition } from "./ContextInspector";

function makeStats(
  overrides: Partial<ContextSnapshotStats> = {},
): ContextSnapshotStats {
  return {
    request_budget_tokens: 10_000,
    estimated_total_tokens: 6_000,
    system_tokens: 2_000,
    project_tokens: 500,
    memory_tokens: 500,
    session_tokens: 0,
    task_tokens: 3_000,
    repo_map_tokens: 500,
    artifact_summary_tokens: 0,
    omitted_tokens: 0,
    compact_triggered: false,
    compact_reason: "",
    compact_method: "",
    compact_truncated: false,
    compact_source_range: null,
    ...overrides,
  };
}

describe("deriveContextComposition", () => {
  it("does not double count repo map as a separate segment", () => {
    const composition = deriveContextComposition(makeStats());

    expect(composition.segments.map((segment) => segment.key)).toEqual([
      "system",
      "memory",
      "task",
      "other",
    ]);
    expect(composition.segments.reduce(
      (total, segment) => total + segment.tokens,
      0,
    )).toBe(6_000);
  });

  it("classifies budget pressure from measured utilization", () => {
    expect(deriveContextComposition(makeStats()).pressure).toBe("low");
    expect(deriveContextComposition(makeStats({
      estimated_total_tokens: 7_500,
    })).pressure).toBe("moderate");
    expect(deriveContextComposition(makeStats({
      estimated_total_tokens: 9_000,
    })).pressure).toBe("high");
    expect(deriveContextComposition(makeStats({
      estimated_total_tokens: 9_700,
    })).pressure).toBe("critical");
    expect(deriveContextComposition(makeStats({
      estimated_total_tokens: 11_000,
    })).pressure).toBe("over");
  });
});
