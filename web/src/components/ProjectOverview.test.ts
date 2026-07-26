import { evidenceLabel } from "./ProjectOverview";

if (evidenceLabel("observed") !== "Observed evidence") {
  throw new Error("Observed capabilities must be distinguished from configuration");
}
if (evidenceLabel("configured") !== "Configured") {
  throw new Error("Configured capabilities must not be described as observed");
}
if (evidenceLabel("missing") !== "Evidence unavailable") {
  throw new Error("Unknown evidence states must fail visibly");
}
