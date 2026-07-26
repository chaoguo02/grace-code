import { describe, expect, it } from "vitest";
import type { EvaluationRun } from "../types/evaluations";
import { deriveEvaluationTrend } from "./EvaluationLab";

function makeRun(
  id: string,
  passRate: number,
  averageTokens: number,
  regressed = false,
): EvaluationRun {
  return {
    id,
    label: id,
    created_at: "2026-07-26T00:00:00Z",
    path: `${id}/validation-report.json`,
    all_passed: passRate === 1,
    pass_rate: passRate,
    passed_count: Math.round(passRate * 2),
    scenario_count: 2,
    average_tokens: averageTokens,
    total_tokens: averageTokens * 2,
    average_steps: 3,
    results: [],
    configuration: {
      provider: "openai",
      model: "model",
      prompt_source: "local",
      prompt_label: "production",
      prompt_version: 1,
    },
    comparison: {
      passed: !regressed,
      checks: [
        {
          name: "tokens",
          passed: !regressed,
          details: "token threshold",
        },
      ],
      metadata: {},
    },
    comparison_source: "computed",
  };
}

describe("deriveEvaluationTrend", () => {
  it("orders oldest to newest and normalizes token bars", () => {
    const trend = deriveEvaluationTrend([
      makeRun("new", 1, 1500),
      makeRun("old", 0.5, 1000),
    ]);

    expect(trend.map((point) => point.id)).toEqual(["old", "new"]);
    expect(trend[0].passPercent).toBe(50);
    expect(trend[1].tokenPercent).toBe(100);
  });

  it("marks points with failed baseline checks as regressed", () => {
    const trend = deriveEvaluationTrend([
      makeRun("regression", 1, 1400, true),
    ]);

    expect(trend[0].regressed).toBe(true);
  });
});
