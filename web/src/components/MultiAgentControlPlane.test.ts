import { orderTopologyNodes } from "./MultiAgentControlPlane";
import { normalizeMultiAgentSnapshot } from "../api/multiAgent";
import type { MultiAgentNode } from "../types/multiAgent";
import type { MultiAgentSnapshot } from "../types/multiAgent";

const base: MultiAgentNode = {
  id: "root",
  parent_id: null,
  agent_name: "build",
  title: "",
  status: "completed",
  agent_kind: "primary",
  context_origin: "fresh",
  execution_placement: "foreground",
  workspace_mode: "current",
  depth: 0,
  generation: 0,
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
  completed_at: null,
  selected: false,
  result_status: null,
};

const ordered = orderTopologyNodes([
  { ...base, id: "grandchild", depth: 2 },
  { ...base, id: "child", depth: 1 },
  base,
]);

if (ordered.map((node) => node.id).join(",") !== "root,child,grandchild") {
  throw new Error("Topology nodes must be presented parent depth first");
}

const normalized = normalizeMultiAgentSnapshot({
  routing_decision: {
    topology: "fan_out",
    reason_code: "independent_work_items",
    explanation: "Two independent scopes",
  },
  tasks: [{
    task_id: "task-1",
    description: "Inspect web",
    assigned_agent: "explore",
    status: "failed",
    depends_on: ["task-0"],
    retry_count: 1,
    error: "provider timeout",
  }],
  team_capability: {
    enabled: true,
    arbitrary_agent_message_bus: true,
  },
} as unknown as MultiAgentSnapshot);

if (normalized.routing?.topology !== "fan_out") {
  throw new Error("Legacy routing_decision must be normalized");
}
if (normalized.delegation_tasks?.[0].dependencies[0] !== "task-0") {
  throw new Error("Task dependency aliases must be normalized");
}
if (!normalized.team?.direct_messaging) {
  throw new Error("Team message-bus capability must be disclosed");
}
