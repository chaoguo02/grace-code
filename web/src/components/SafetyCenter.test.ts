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

if (filterSafetyTools(tools, "high", "").map((tool) => tool.name).join() !== "Edit") {
  throw new Error("Safety tool filtering must respect risk");
}
if (filterSafetyTools(tools, "all", "read_workspace").map((tool) => tool.name).join() !== "Read") {
  throw new Error("Safety tool filtering must search declarative effects");
}
