import { deriveVisibleToolDelta } from "./ReplayLab";

const delta = deriveVisibleToolDelta(
  [
    { name: "Read", visible: true },
    { name: "Edit", visible: true },
  ],
  [
    { name: "Read", visible: true },
    { name: "Bash", visible: true },
  ],
);

if (delta.added.join(",") !== "Edit") {
  throw new Error("Replay visibility delta must identify newly visible tools");
}
if (delta.removed.join(",") !== "Bash") {
  throw new Error("Replay visibility delta must identify removed tools");
}
if (delta.unchanged.join(",") !== "Read") {
  throw new Error("Replay visibility delta must retain stable tools");
}
