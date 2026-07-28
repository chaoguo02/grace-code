import { describe, expect, it } from "vitest";
import { deriveReliabilityBars } from "./ReliabilityDashboard";

describe("deriveReliabilityBars", () => {
  it("normalizes runs and tokens independently", () => {
    const bars = deriveReliabilityBars([
      { date: "2026-07-25", runs: 1, tokens: 50, success_rate: 1 },
      { date: "2026-07-26", runs: 2, tokens: 100, success_rate: 0.5 },
    ]);
    expect(bars.map((item) => item.run_height)).toEqual([50, 100]);
    expect(bars.map((item) => item.token_height)).toEqual([50, 100]);
  });
});
