import { groupComponentsByLayer } from "./ArchitectureExplorer";

const groups = groupComponentsByLayer([
  {
    id: "runtime",
    label: "Runtime",
    layer: "orchestration",
    status: "available",
    responsibility: "Runs sessions",
  },
  {
    id: "web",
    label: "Web",
    layer: "interface",
    status: "available",
    responsibility: "Presents facts",
  },
]);

if (groups.map((group) => group.key).join(",") !== "interface,orchestration") {
  throw new Error("Architecture layers must use the canonical display order");
}
