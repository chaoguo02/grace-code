import { deriveReliabilityBars } from "./ReliabilityDashboard";

const bars = deriveReliabilityBars([
  { date: "2026-07-25", runs: 1, tokens: 50, success_rate: 1 },
  { date: "2026-07-26", runs: 2, tokens: 100, success_rate: 0.5 },
]);

if (bars[1].run_height !== 100 || bars[0].run_height !== 50) {
  throw new Error("Run bars must be normalized to observed volume");
}
if (bars[1].token_height !== 100 || bars[0].token_height !== 50) {
  throw new Error("Token bars must be normalized independently");
}
