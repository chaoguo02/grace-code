import { describe, expect, it } from "vitest";
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

describe("multi-agent projections", () => {
  it("orders nodes parent first", () => {
    const ordered = orderTopologyNodes([
      { ...base, id: "grandchild", depth: 2 },
      { ...base, id: "child", depth: 1 },
      base,
    ]);
    expect(ordered.map((node) => node.id)).toEqual(["root", "child", "grandchild"]);
  });

  it("normalizes legacy routing, dependencies, and messaging", () => {
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
      team_capability: { enabled: true, arbitrary_agent_message_bus: true },
    } as unknown as MultiAgentSnapshot);
    expect(normalized.routing?.topology).toBe("fan_out");
    expect(normalized.delegation_tasks?.[0].dependencies[0]).toBe("task-0");
    expect(normalized.team?.direct_messaging).toBe(true);
  });
});
