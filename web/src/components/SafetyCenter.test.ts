import { describe, expect, it } from "vitest";
import { filterSafetyTools } from "./SafetyCenter";
import type { SafetyTool } from "../types/safety";

const tools: SafetyTool[] = [
  {
    name: "Read",
    risk: "low",
    control: "policy_evaluated",
    effects: ["read_workspace"],
    path_access: "read",
    path_parameter: "path",
    requires_user_interaction: false,
    required_permissions: [],
    matching_rules: [],
  },
  {
    name: "Edit",
    risk: "high",
    control: "ask_rule",
    effects: ["write_workspace"],
    path_access: "write",
    path_parameter: "path",
    requires_user_interaction: false,
    required_permissions: [],
    matching_rules: [],
  },
];

describe("filterSafetyTools", () => {
  it("filters by risk and declarative effects", () => {
    expect(filterSafetyTools(tools, "high", "").map((tool) => tool.name)).toEqual(["Edit"]);
    expect(filterSafetyTools(tools, "all", "read_workspace").map((tool) => tool.name)).toEqual(["Read"]);
  });
});
